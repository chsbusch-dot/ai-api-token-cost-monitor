"""Tests for per-source spend attribution: Anthropic per-API-key, OpenAI
per-project, Gemini per-ingest-source."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from costwatch import store
from costwatch.core.billing import BillingState
from costwatch.core.billing_fetchers import _compute_today_cost_from_usage, _sum_openai_costs
from costwatch.store import read_attribution, record_usage, today_usd_by_gemini_source, write_snapshot


def test_usage_compute_returns_total_and_per_key_split():
    data = {"data": [{"results": [
        {"model": "claude-haiku-4-5", "api_key_id": "apikey_A",
         "uncached_input_tokens": 1_000_000, "output_tokens": 0},
        {"model": "claude-haiku-4-5", "api_key_id": "apikey_B",
         "uncached_input_tokens": 0, "output_tokens": 1_000_000},
        {"model": "claude-haiku-4-5", "api_key_id": None,   # Workbench/console
         "uncached_input_tokens": 2_000_000, "output_tokens": 0},
    ]}]}
    total, by_key = _compute_today_cost_from_usage(data)
    # haiku: $1/MTok in, $5/MTok out
    assert abs(by_key["apikey_A"] - 1.00) < 1e-9
    assert abs(by_key["apikey_B"] - 5.00) < 1e-9
    assert abs(by_key[None] - 2.00) < 1e-9
    assert abs(total - 8.00) < 1e-9


def test_openai_costs_split_by_project():
    data = {"data": [{"results": [
        {"amount": {"value": 1.5, "currency": "usd"}, "project_id": "proj_x"},
        {"amount": {"value": 0.5, "currency": "usd"}, "project_id": "proj_x"},
        {"amount": {"value": 2.0, "currency": "usd"}, "project_id": "proj_y"},
        {"amount": {"value": 0.25, "currency": "usd"}, "project_id": None},
    ]}]}
    total, by_project = _sum_openai_costs(data)
    assert abs(total - 4.25) < 1e-9
    assert abs(by_project["proj_x"] - 2.0) < 1e-9
    assert abs(by_project["proj_y"] - 2.0) < 1e-9
    assert abs(by_project[None] - 0.25) < 1e-9


def test_write_snapshot_persists_positive_sources_only():
    state = BillingState()
    state.fresh_today = {"anthropic": 3.0}
    state.fresh_sources = {
        "anthropic:Recorderbot-key": 2.5,
        "anthropic:adhoc-key": 0.0,       # zero → skipped (sparse table)
        "openai:Shell": 0.5,
    }
    n = write_snapshot(state)
    assert n == 3  # anthropic total + 2 nonzero sources
    with store._conn() as c:
        provs = {r[0] for r in c.execute("SELECT provider FROM snapshots")}
    assert "anthropic:Recorderbot-key" in provs
    assert "openai:Shell" in provs
    assert "anthropic:adhoc-key" not in provs


def test_gemini_rollup_groups_by_source_with_untagged_default():
    record_usage("gemini", "gemini-2.5-pro", 1_000_000, 0, source="recorderbot")
    record_usage("gemini", "gemini-2.5-pro", 0, 1_000_000, source=None)
    by_source = today_usd_by_gemini_source()
    # 2.5-pro: $1.25/MTok in, $10/MTok out
    assert abs(by_source["recorderbot"] - 1.25) < 1e-9
    assert abs(by_source["untagged"] - 10.00) < 1e-9


def test_read_attribution_orders_by_total_and_shapes_series():
    now = int(time.time())
    with store._conn() as c:
        store.init()
        c.executemany(
            "INSERT OR REPLACE INTO snapshots (ts, provider, today_usd, balance_usd, by_model) "
            "VALUES (?, ?, ?, NULL, NULL)",
            [
                (now - 120, "anthropic:small-key", 0.10),
                (now - 60, "anthropic:small-key", 0.20),
                (now - 120, "openai:BigProject", 4.00),
                (now - 60, "openai:BigProject", 5.00),
            ],
        )
    report = read_attribution(days=2)
    assert [s["source"] for s in report["sources"]] == ["openai:BigProject", "anthropic:small-key"]
    big = report["sources"][0]
    assert set(big.keys()) == {"source", "today", "total", "series"}
    assert all(set(e.keys()) == {"date", "usd"} for e in big["series"])
    assert len(big["series"]) == 2


def test_attribution_endpoint_returns_safe_fields_only():
    from costwatch.web.server import app
    with TestClient(app) as client:
        r = client.get("/api/attribution?days=7")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"days", "sources"}
        for s in body["sources"]:
            assert set(s.keys()) == {"source", "today", "total", "series"}
