"""SQLite-backed usage store.

Two tables:
  - usage_reports: token records ingested locally (Gemini, custom models)
  - snapshots:     one row per provider per poll tick (history of API-reported
                   totals; persists across daemon restarts)
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .core.billing import BillingState, PRICING_USD_PER_MTOK, DEFAULT_PRICE

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "costwatch.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_reports (
    ts            INTEGER NOT NULL,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    source        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (ts, provider, model, source)
);
CREATE INDEX IF NOT EXISTS idx_usage_provider_ts ON usage_reports(provider, ts);

-- One row per (poll-tick, provider). today_usd for spend providers,
-- balance_usd for prepaid providers (deepgram). Mutually exclusive per row.
CREATE TABLE IF NOT EXISTS snapshots (
    ts          INTEGER NOT NULL,
    provider    TEXT    NOT NULL,
    today_usd   REAL,
    balance_usd REAL,
    by_model    TEXT,
    PRIMARY KEY (ts, provider)
);
CREATE INDEX IF NOT EXISTS idx_snap_provider_ts ON snapshots(provider, ts DESC);
"""

_initialized = False


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=5.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init() -> None:
    global _initialized
    if _initialized:
        return
    with _conn() as c:
        c.executescript(_SCHEMA)
    _initialized = True


def record_usage(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    source: Optional[str] = None,
) -> None:
    """Append a token usage record. Same-second collisions accumulate."""
    init()
    ts = int(time.time())
    with _conn() as c:
        c.execute(
            """
            INSERT INTO usage_reports (ts, provider, model, input_tokens, output_tokens, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts, provider, model, source) DO UPDATE SET
                input_tokens  = input_tokens  + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens
            """,
            (ts, provider, model, int(input_tokens), int(output_tokens), source or ""),
        )


def write_snapshot(state: BillingState) -> int:
    """Persist the current BillingState as one row per provider. Returns row count."""
    init()
    ts = int(time.time())
    by_model_json = (
        json.dumps({
            m: {"in": s.input_tokens, "out": s.output_tokens, "usd": round(s.cost_usd(m), 5)}
            for m, s in state.by_model.items()
        })
        if state.by_model else None
    )
    rows: list[tuple[int, str, Optional[float], Optional[float], Optional[str]]] = []
    if state.anthropic_today_usd is not None:
        rows.append((ts, "anthropic", state.anthropic_today_usd, None, by_model_json))
    if state.openai_today_usd is not None:
        rows.append((ts, "openai", state.openai_today_usd, None, by_model_json))
    if state.gemini_today_usd is not None:
        rows.append((ts, "gemini", state.gemini_today_usd, None, by_model_json))
    if state.deepgram_balance_usd is not None:
        rows.append((ts, "deepgram", None, state.deepgram_balance_usd, None))

    if not rows:
        return 0
    with _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO snapshots (ts, provider, today_usd, balance_usd, by_model) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def read_history(days: int = 14) -> dict[str, Any]:
    """Per-day per-provider totals for the last N UTC days (most recent snapshot wins)."""
    init()
    from datetime import timedelta
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days)

    with _conn() as c:
        rows = c.execute(
            """
            SELECT
                strftime('%Y-%m-%d', ts, 'unixepoch') AS date,
                provider, today_usd, balance_usd, ts
            FROM snapshots
            WHERE ts >= ? AND ts < ?
            ORDER BY ts ASC
            """,
            (int(start.timestamp()), int(end.timestamp())),
        ).fetchall()

    # Fold to most-recent per (date, provider).
    fold: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {}
    for date, provider, today, balance, _ts in rows:
        fold[(date, provider)] = (today, balance)

    dates = sorted({d for (d, _) in fold})
    series = []
    for d in dates:
        entry: dict[str, Any] = {"date": d}
        for prov in ("anthropic", "openai", "gemini"):
            today, _ = fold.get((d, prov), (None, None))
            entry[prov] = round(today, 4) if today is not None else 0.0
        _, balance = fold.get((d, "deepgram"), (None, None))
        entry["deepgram_balance"] = round(balance, 2) if balance is not None else None
        entry["total_spend"] = round(entry["anthropic"] + entry["openai"] + entry["gemini"], 4)
        series.append(entry)

    return {"days": days, "series": series}


def latest_snapshots() -> dict[str, dict[str, Any]]:
    """Return the most recent snapshot per provider as a dict keyed by provider."""
    init()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT s.provider, s.ts, s.today_usd, s.balance_usd, s.by_model
            FROM snapshots s
            WHERE s.ts = (SELECT MAX(ts) FROM snapshots s2 WHERE s2.provider = s.provider)
            """
        ).fetchall()
    return {
        provider: {"ts": ts, "today_usd": today, "balance_usd": balance, "by_model": by_model}
        for provider, ts, today, balance, by_model in rows
    }


def today_usd_by_provider(provider: str) -> float:
    """Sum today's UTC usage rows × local pricing for the given provider."""
    init()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(today.timestamp())
    with _conn() as c:
        rows = c.execute(
            """
            SELECT model, COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
            FROM usage_reports
            WHERE provider = ? AND ts >= ?
            GROUP BY model
            """,
            (provider, start),
        ).fetchall()
    total = 0.0
    for model, in_tok, out_tok in rows:
        in_price, out_price = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICE)
        total += (in_tok * in_price + out_tok * out_price) / 1_000_000
    return total
