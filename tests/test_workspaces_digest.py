"""The digest's Claude-by-workspace section (Admin API usage report) and its Telegram copy.

No network: the Admin API is an httpx.MockTransport, Telegram's post is monkeypatched.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from costwatch import telegram, workspaces
from costwatch.digest import render_html, render_text
from costwatch.web import server as srv

FAKE_ADMIN = "sk-ant-admin-FAKEWSDIGEST-1234567890ABCDEF1234567890ABCDEF1234567890"
FAKE_BOT = "123456:FAKEbotTOKENdoNotLeak"
NOW = datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc)
OPUS_1M_IN = {"model": "claude-opus-5-5", "uncached_input_tokens": 1_000_000, "output_tokens": 0,
              "cache_read_input_tokens": 0, "cache_creation": {}}


def _admin(handler_calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET" and req.headers["x-api-key"] == FAKE_ADMIN
        q = parse_qs(urlparse(str(req.url)).query)
        handler_calls.append((req.url.path, q))
        if req.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"data": [
                {"id": "wrkspc_inoc", "name": "inoculis-bo"},
                {"id": "wrkspc_saw", "name": "Sawyer <Teleprompter>"}], "has_more": False, "last_id": None})
        assert q["group_by[]"] == ["workspace_id", "model"]
        if q["bucket_width"] == ["1h"]:
            data = [{"starting_at": "2026-10-27T15:00:00Z", "results": [
                {**OPUS_1M_IN, "workspace_id": "wrkspc_inoc"},
                {**OPUS_1M_IN, "workspace_id": None, "uncached_input_tokens": 250_000}]}]
            return httpx.Response(200, json={"data": data, "has_more": False, "next_page": None})
        if not q.get("page"):
            data = [{"starting_at": "2026-10-24T00:00:00Z", "results": [
                {**OPUS_1M_IN, "workspace_id": "wrkspc_saw", "uncached_input_tokens": 5_000_000}]}]
            return httpx.Response(200, json={"data": data, "has_more": True, "next_page": "p2"})
        data = [{"starting_at": "2026-10-27T00:00:00Z", "results": [
            {**OPUS_1M_IN, "workspace_id": "wrkspc_inoc"}, {**OPUS_1M_IN, "workspace_id": None,
                                                           "uncached_input_tokens": 250_000}]}]
        return httpx.Response(200, json={"data": data, "has_more": False, "next_page": None})
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"x-api-key": FAKE_ADMIN})


def test_build_names_prices_and_sorts_workspaces(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CREDIT_CYCLE_DAY", "24")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_CREDITS_USD", "200")
    calls: list = []
    ws = workspaces.build(NOW, admin_key=FAKE_ADMIN, client=_admin(calls))
    assert ws["cycle_start"] == "2026-10-24"
    assert [r["workspace"] for r in ws["rows"]] == ["Sawyer <Teleprompter>", "inoculis-bo", "Default"]
    by = {r["workspace"]: r for r in ws["rows"]}
    assert by["inoculis-bo"]["today_usd"] == 4.0 and by["inoculis-bo"]["cycle_usd"] == 4.0
    assert by["Default"]["today_usd"] == 1.0 and by["Default"]["today_tokens"] == 250_000
    assert by["Sawyer <Teleprompter>"]["today_usd"] == 0 and by["Sawyer <Teleprompter>"]["cycle_usd"] == 20.0
    assert ws["cycle_total"] == 25.0 and ws["credits_usd"] == 200.0 and ws["today_total"] == 5.0
    cycle_q = [q for path, q in calls if q.get("bucket_width") == ["1d"]][0]
    assert cycle_q["starting_at"] == ["2026-10-24T00:00:00Z"] and cycle_q["ending_at"] == ["2026-10-27T18:00:00Z"]


def test_build_is_none_without_key_or_when_the_api_refuses(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    assert workspaces.build(NOW) is None
    refusing = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={})))
    assert workspaces.build(NOW, admin_key=FAKE_ADMIN, client=refusing) is None
    assert workspaces.build(NOW, admin_key=FAKE_ADMIN) is None  # conftest: dead loopback port


@pytest.mark.parametrize("now,day,expect", [
    (datetime(2026, 10, 27, tzinfo=timezone.utc), 24, "2026-10-24"),
    (datetime(2026, 10, 20, tzinfo=timezone.utc), 24, "2026-09-24"),
    (datetime(2026, 1, 5, tzinfo=timezone.utc), 24, "2025-12-24"),
    (datetime(2026, 10, 27, tzinfo=timezone.utc), 1, "2026-10-01"),
    (datetime(2026, 3, 30, tzinfo=timezone.utc), 31, "2026-03-28"),
])
def test_cycle_start(now, day, expect):
    assert workspaces.cycle_start(now, day).strftime("%Y-%m-%d") == expect


def _payload(ws):
    return {"date": "2026-10-27", "total_today": 5.0, "total_avg7": 3.0,
            "by_provider": {"anthropic": {"today": 5.0, "avg7": 3.0, "delta_pct": 66.7}},
            "deepgram_balance": None, "top_models": [], "by_workspace": ws,
            "dashboard_url": "http://example.com"}


def test_renderers_show_the_section_and_escape_names(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CREDIT_CYCLE_DAY", "24")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_CREDITS_USD", "200")
    ws = workspaces.build(NOW, admin_key=FAKE_ADMIN, client=_admin([]))
    text = render_text(_payload(ws))
    assert "Claude by workspace (today / since 2026-10-24, list price):" in text
    assert "credits: $25.00 of $200 used this cycle (12%)" in text
    html = render_html(_payload(ws))
    assert "Sawyer &lt;Teleprompter&gt;" in html and "Sawyer <Teleprompter>" not in html
    assert "Claude by workspace" not in render_text(_payload(None))


def test_telegram_off_unless_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert telegram.send("hi") is False


def test_telegram_errors_never_carry_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_BOT)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = {}

    def ok(url, **kw):
        sent.update(kw["data"])
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(telegram.httpx, "post", ok)
    assert telegram.send("x" * 5000) is True and len(sent["text"]) <= telegram.MAX_LEN

    monkeypatch.setattr(telegram.httpx, "post", lambda url, **kw: httpx.Response(401, json={"ok": False}))
    with pytest.raises(telegram.TelegramError) as e:
        telegram.send("hi")
    assert FAKE_BOT not in str(e.value) and "401" in str(e.value)

    def boom(url, **kw):
        raise httpx.ConnectError(f"cannot reach {url}")
    monkeypatch.setattr(telegram.httpx, "post", boom)
    with pytest.raises(telegram.TelegramError) as e:
        telegram.send("hi")
    assert FAKE_BOT not in str(e.value)


def test_admin_digest_sends_telegram_and_survives_its_failure(monkeypatch):
    monkeypatch.setattr(srv, "_require_loopback", lambda req: None)
    monkeypatch.setattr(srv, "send_mail", lambda *a, **k: None)
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", FAKE_ADMIN)
    posted = []
    monkeypatch.setattr(srv.telegram, "send", lambda text: posted.append(text) or True)
    with TestClient(srv.app) as client:
        r = client.post("/admin/digest")
    assert r.status_code == 200 and r.json()["telegram"] == "sent"
    assert posted and FAKE_ADMIN not in posted[0] and "costwatch" in posted[0]

    def fail(text):
        raise telegram.TelegramError("telegram: HTTP 401")
    monkeypatch.setattr(srv.telegram, "send", fail)
    with TestClient(srv.app) as client:
        r = client.post("/admin/digest")
    assert r.status_code == 200 and r.json()["telegram"] == "failed"
    assert json.dumps(r.json()).count(FAKE_ADMIN) == 0
