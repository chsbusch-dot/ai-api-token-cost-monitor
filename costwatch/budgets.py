"""Budget threshold alerts.

Configured via env vars:
    BUDGET_ANTHROPIC=25:80,100      # daily $25, alert at 80% and 100%
    BUDGET_OPENAI=10:80,100
    BUDGET_GEMINI=5:100

Dedup: alerts_sent table records (date, provider, threshold_pct) so each
threshold fires at most once per UTC day, surviving daemon restarts.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from .store import _conn, init

log = logging.getLogger(__name__)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
    for prov in ("anthropic", "openai", "gemini"):
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


def evaluate(state) -> list[dict]:
    """Returns alerts that just tripped (and records them in alerts_sent)."""
    budgets = _budgets_from_env()
    if not budgets:
        return []
    _ensure_table()
    today = _today_utc()
    fires: list[dict] = []

    for budget in budgets:
        prov = budget["provider"]
        limit = budget["daily_usd"]
        spend = getattr(state, f"{prov}_today_usd", None)
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
                c.execute(
                    "INSERT INTO alerts_sent (date, provider, threshold, sent_at) VALUES (?, ?, ?, ?)",
                    (today, prov, pct, int(time.time())),
                )
            fires.append(
                {
                    "provider": prov,
                    "threshold_pct": pct,
                    "spend_usd": round(spend, 4),
                    "limit_usd": round(limit, 2),
                }
            )
    return fires
