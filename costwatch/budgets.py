"""Budget threshold alerts.

Configured via env vars:
    BUDGET_ANTHROPIC=25:80,100      # daily $25, alert at 80% and 100%
    BUDGET_OPENAI=10:80,100
    BUDGET_GEMINI=5:100

Budgets are evaluated against LOCAL-calendar-day spend. Dedup: alerts_sent
records (local_date, provider, threshold_pct) so each threshold fires at most
once per local day, surviving daemon restarts.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from .store import _conn, init

log = logging.getLogger(__name__)


def _today_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _ensure_table() -> None:
    init()
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts_sent (
                date      TEXT    NOT NULL,
                provider  TEXT    NOT NULL,
                threshold REAL    NOT NULL,
                sent_at   INTEGER NOT NULL,
                PRIMARY KEY (date, provider, threshold)
            )
            """
        )


def _budgets_from_env() -> list[dict]:
    out = []
    for prov in ("anthropic", "claude_code", "openai", "gemini"):
        raw = os.getenv(f"BUDGET_{prov.upper()}", "").strip()
        if not raw:
            continue
        try:
            if ":" in raw:
                limit_str, pcts_str = raw.split(":", 1)
            else:
                limit_str, pcts_str = raw, "100"
            limit = float(limit_str)
            pcts = sorted({float(p) for p in pcts_str.split(",") if p.strip()})
            if limit > 0 and pcts:
                out.append({"provider": prov, "daily_usd": limit, "pcts": pcts})
        except ValueError:
            log.warning("ignoring malformed BUDGET_%s=%r", prov.upper(), raw)
    return out


def evaluate(spend_by_provider: dict[str, float], date_str: str | None = None) -> list[dict]:
    """Returns alerts that just tripped. Does NOT record them — the caller must
    call record_sent() after the alert email actually goes out, so a transient
    SMTP failure retries on the next tick instead of permanently swallowing
    the alert.

    `spend_by_provider` is the LOCAL-calendar-day spend per provider (see
    store.local_today_spend); dedup is keyed by the local date so each
    threshold fires at most once per local day.
    """
    budgets = _budgets_from_env()
    if not budgets:
        return []
    _ensure_table()
    today = date_str or _today_local()
    fires: list[dict] = []

    for budget in budgets:
        prov = budget["provider"]
        limit = budget["daily_usd"]
        spend = spend_by_provider.get(prov)
        if spend is None:
            continue
        for pct in budget["pcts"]:
            if spend < (limit * pct / 100):
                continue
            with _conn() as c:
                already = c.execute(
                    "SELECT 1 FROM alerts_sent WHERE date=? AND provider=? AND threshold=?",
                    (today, prov, pct),
                ).fetchone()
            if already:
                continue
            fires.append(
                {
                    "provider": prov,
                    "threshold_pct": pct,
                    "spend_usd": round(spend, 4),
                    "limit_usd": round(limit, 2),
                    "date": today,
                }
            )
    return fires


def record_sent(fire: dict) -> None:
    """Record a successfully-delivered alert so it never fires again that day.
    Call ONLY after the email send succeeded."""
    _ensure_table()
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO alerts_sent (date, provider, threshold, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (fire["date"], fire["provider"], fire["threshold_pct"], int(time.time())),
        )
