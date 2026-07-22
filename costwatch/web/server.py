"""FastAPI app: SSE live spend, history JSON, ingest endpoint, dashboard HTML."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..budgets import evaluate as evaluate_budgets, record_sent as record_alert_sent
from ..core.billing import BillingTracker
from ..core.billing_fetchers import billing_poller
from ..digest import build_digest_payload, render_html, render_text
from ..events import EventBus
from ..mailer import MailError, send as send_mail
from ..store import (
    SPEND_PROVIDERS,
    local_today_spend,
    read_attribution,
    read_history,
    record_usage,
    write_snapshot,
)

WEB_DIR = Path(__file__).parent
log = logging.getLogger("costwatch.web")


class UsageReport(BaseModel):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    source: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus = EventBus()
    tracker = BillingTracker()
    stop = asyncio.Event()
    poller_task = None

    async def on_update(state):
        write_snapshot(state)
        # User-facing "today" is the LOCAL calendar day, reconstructed from
        # snapshot deltas — the raw state fields are vendor UTC-day counters,
        # which are wrong for display in any non-UTC timezone (at 18:00 PDT
        # the UTC day is one hour old).
        local_spend = {p: local_today_spend(p) for p in SPEND_PROVIDERS}
        payload = state.to_payload()
        for p in SPEND_PROVIDERS:
            payload[f"{p}_today_usd"] = round(local_spend[p], 4)
        payload["total_today_usd"] = round(sum(local_spend.values()), 4)
        await bus.publish(payload)
        # Budget alerts (no-op if no BUDGET_* env vars configured). The dedup
        # record is written only after a successful send — SMTP hiccups retry
        # on the next tick instead of permanently swallowing the alert.
        try:
            for fire in evaluate_budgets(local_spend):
                try:
                    _send_budget_alert(fire)
                except Exception:
                    log.exception("budget alert send failed (%s %s%%) — will retry next tick",
                                  fire["provider"], fire["threshold_pct"])
                else:
                    record_alert_sent(fire)
        except Exception:
            log.exception("budget evaluation failed")

    if not os.getenv("COSTWATCH_TESTING"):
        poller_task = asyncio.create_task(
            billing_poller(
                tracker=tracker,
                anthropic_admin_key=os.getenv("ANTHROPIC_ADMIN_API_KEY") or None,
                openai_admin_key=os.getenv("OPENAI_ADMIN_API_KEY") or None,
                deepgram_api_key=os.getenv("DEEPGRAM_ADMIN_API_KEY") or None,
                anthropic_org_id=os.getenv("ANTHROPIC_ORG_ID") or None,
                anthropic_session_cookie=os.getenv("ANTHROPIC_SESSION_COOKIE") or None,
                on_update=on_update,
                interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
                stop=stop,
            ),
            name="billing_poller",
        )
        log.info("poller started")
    app.state.bus = bus
    app.state.tracker = tracker
    try:
        yield
    finally:
        stop.set()
        if poller_task is not None:
            try:
                await asyncio.wait_for(poller_task, timeout=10)
            except asyncio.TimeoutError:
                poller_task.cancel()
            log.info("poller stopped")


app = FastAPI(title="costwatch", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/now")
async def now(request: Request):
    """Server-Sent Events stream of BillingState payloads (one per poll tick)."""
    bus: EventBus = request.app.state.bus

    async def stream():
        q = await bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"event: spend\ndata: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
async def history(days: int = 14):
    if not 1 <= days <= 90:
        raise HTTPException(400, "days must be 1..90")
    return read_history(days)


@app.get("/api/attribution")
async def attribution(days: int = 14):
    """Per-source local-day spend: Anthropic API keys, OpenAI projects,
    Gemini ingest source tags."""
    if not 1 <= days <= 90:
        raise HTTPException(400, "days must be 1..90")
    return read_attribution(days)


@app.post("/api/usage")
async def ingest_usage(report: UsageReport):
    record_usage(
        provider=report.provider,
        model=report.model,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        source=report.source,
    )
    return {"ok": True}


# ── Loopback-only admin endpoints ──────────────────────────────────────────


def _require_loopback(request: Request) -> None:
    host = request.client.host if request.client else None
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "loopback only")


def _send_budget_alert(fire: dict) -> None:
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:8770").rstrip("/")
    subj = (
        f"costwatch · ALERT · {fire['provider']} {fire['threshold_pct']:.0f}% "
        f"of daily budget (${fire['spend_usd']:.4f} / ${fire['limit_usd']:.2f})"
    )
    text = (
        f"Provider: {fire['provider']}\n"
        f"Spend today: ${fire['spend_usd']:.4f}\n"
        f"Daily budget: ${fire['limit_usd']:.2f}\n"
        f"Threshold tripped: {fire['threshold_pct']:.0f}%\n\n"
        f"Dashboard: {dashboard_url}\n"
    )
    html = (
        f'<h2 style="color:#d97706;">Budget alert · {fire["provider"]}</h2>'
        f'<p><strong>${fire["spend_usd"]:.4f}</strong> spent today, '
        f'<strong>{fire["threshold_pct"]:.0f}%</strong> of daily budget '
        f'(${fire["limit_usd"]:.2f}).</p>'
        f'<p><a href="{dashboard_url}">Open dashboard →</a></p>'
    )
    send_mail(subj, html, text)


@app.post("/admin/digest")
async def admin_digest(request: Request):
    _require_loopback(request)
    p = build_digest_payload()
    subject = f"costwatch · {p['date']} · ${p['total_today']:.4f} total"
    try:
        send_mail(subject, render_html(p), render_text(p))
    except MailError as e:
        raise HTTPException(500, f"mail config: {e}")
    except Exception as e:
        raise HTTPException(502, f"smtp error: {e}")
    return {"ok": True, "to": os.getenv("DIGEST_TO"), "total_today": p["total_today"]}


@app.post("/admin/test_email")
async def admin_test_email(request: Request):
    _require_loopback(request)
    try:
        send_mail(
            "costwatch · test email",
            "<p>If you can read this, SMTP works. ✓</p>",
            "If you can read this, SMTP works.",
        )
    except MailError as e:
        raise HTTPException(500, f"mail config: {e}")
    except Exception as e:
        raise HTTPException(502, f"smtp error: {e}")
    return {"ok": True, "to": os.getenv("DIGEST_TO")}
