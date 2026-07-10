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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .core.billing import BillingState, PRICING_USD_PER_MTOK, DEFAULT_PRICE

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "costwatch.db"

SPEND_PROVIDERS = ("anthropic", "claude_code", "openai", "gemini")

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
    """Persist the current BillingState as one row per provider. Returns row count.

    Spend providers are written from `state.fresh_today` — the values actually
    fetched THIS tick — never from the last-known display fields. A failed
    fetch therefore produces a gap, not a stale row; gaps are harmless to the
    delta-sum accounting below, whereas a stale cumulative counter carried
    across UTC midnight would be double-counted as new-day spend.
    """
    init()
    ts = int(time.time())
    by_model_json = (
        json.dumps({
            m: {"in": s.input_tokens, "out": s.output_tokens, "usd": round(s.cost_usd(m), 5)}
            for m, s in state.by_model.items()
        })
        if state.by_model else None
    )
    fresh = getattr(state, "fresh_today", None) or {}
    rows: list[tuple[int, str, Optional[float], Optional[float], Optional[str]]] = []
    for prov in SPEND_PROVIDERS:
        val = fresh.get(prov)
        if val is not None:
            rows.append((ts, prov, float(val), None, by_model_json))
    # Per-source attribution counters ("anthropic:Recorderbot-key", ...).
    # Zero-valued sources are skipped to keep the table sparse — a day with no
    # rows for a source reads as $0 in the delta-sum accounting anyway.
    for src, val in (getattr(state, "fresh_sources", None) or {}).items():
        if val is not None and val > 0:
            rows.append((ts, src, float(val), None, None))
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


def _tzinfo(tz=None):
    """Resolve the reporting timezone: explicit arg > system local zone."""
    return tz or datetime.now().astimezone().tzinfo


def spend_between(provider: str, start_ts: int, end_ts: int, now_ts: Optional[int] = None) -> float:
    """True USD spend in [start_ts, end_ts) computed from snapshot deltas.

    Vendor counters (`today_usd`) are cumulative within a UTC day and reset at
    UTC midnight. Summing consecutive-snapshot deltas — treating a UTC-day
    boundary as a reset — converts them into spend attributable to any
    arbitrary window, e.g. a LOCAL calendar day. Robust to snapshot gaps
    (delta across a gap within the same UTC day is exact) and to small
    downward revisions (clamped to 0).
    """
    init()
    end_ts = min(end_ts, (now_ts or int(time.time())) + 1)
    lookback = start_ts - 26 * 3600  # enough to find an anchor in the same UTC day
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, today_usd FROM snapshots "
            "WHERE provider = ? AND ts >= ? AND ts < ? AND today_usd IS NOT NULL "
            "ORDER BY ts",
            (provider, lookback, end_ts),
        ).fetchall()
    total = 0.0
    prev_ts: Optional[int] = None
    prev_val: Optional[float] = None
    for ts, val in rows:
        if prev_ts is not None and ts // 86400 == prev_ts // 86400:
            delta = max(0.0, val - prev_val)
        else:
            # First snapshot ever, or first after UTC-midnight reset: the
            # counter itself is the spend since the reset.
            delta = max(0.0, val)
        if ts >= start_ts:
            total += delta
        prev_ts, prev_val = ts, val
    return total


def daily_spend_series(
    provider: str, days: int, tz=None, now_ts: Optional[int] = None
) -> list[tuple[str, float]]:
    """[(local_date, usd), ...] for the last `days` LOCAL calendar days,
    oldest first; the final entry is today (partial). Days without spend are 0.0."""
    init()
    tzi = _tzinfo(tz)
    now = datetime.fromtimestamp(now_ts, tzi) if now_ts else datetime.now(tzi)
    first = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(first.timestamp())
    lookback = start_ts - 26 * 3600

    totals: dict[str, float] = {}
    for i in range(days):
        totals[(first + timedelta(days=i)).strftime("%Y-%m-%d")] = 0.0

    with _conn() as c:
        rows = c.execute(
            "SELECT ts, today_usd FROM snapshots "
            "WHERE provider = ? AND ts >= ? AND ts <= ? AND today_usd IS NOT NULL "
            "ORDER BY ts",
            (provider, lookback, int(now.timestamp())),
        ).fetchall()

    prev_ts: Optional[int] = None
    prev_val: Optional[float] = None
    for ts, val in rows:
        if prev_ts is not None and ts // 86400 == prev_ts // 86400:
            delta = max(0.0, val - prev_val)
        else:
            delta = max(0.0, val)
        if ts >= start_ts:
            key = datetime.fromtimestamp(ts, tzi).strftime("%Y-%m-%d")
            if key in totals:
                totals[key] += delta
        prev_ts, prev_val = ts, val

    return [(d, round(v, 6)) for d, v in totals.items()]


def local_today_spend(provider: str, tz=None, now_ts: Optional[int] = None) -> float:
    """USD spent so far during the current LOCAL calendar day."""
    return daily_spend_series(provider, 1, tz=tz, now_ts=now_ts)[-1][1]


def read_history(days: int = 14, tz=None) -> dict[str, Any]:
    """Per-LOCAL-day per-provider spend for the last N days, plus the last
    Deepgram balance reading of each day. Every day in the range is emitted."""
    init()
    tzi = _tzinfo(tz)
    series_by_prov = {p: dict(daily_spend_series(p, days, tz=tzi)) for p in SPEND_PROVIDERS}
    dates = list(next(iter(series_by_prov.values())).keys())

    # Last Deepgram balance reading per local day.
    first_ts = int(
        (datetime.now(tzi) - timedelta(days=days - 1))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    balances: dict[str, float] = {}
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, balance_usd FROM snapshots "
            "WHERE provider = 'deepgram' AND ts >= ? AND balance_usd IS NOT NULL ORDER BY ts",
            (first_ts,),
        ).fetchall()
    for ts, bal in rows:
        balances[datetime.fromtimestamp(ts, tzi).strftime("%Y-%m-%d")] = bal

    series = []
    for d in dates:
        entry: dict[str, Any] = {"date": d}
        for prov in SPEND_PROVIDERS:
            entry[prov] = round(series_by_prov[prov].get(d, 0.0), 4)
        bal = balances.get(d)
        entry["deepgram_balance"] = round(bal, 2) if bal is not None else None
        entry["total_spend"] = round(sum(entry[p] for p in SPEND_PROVIDERS), 4)
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


def today_usd_by_gemini_source(provider: str = "gemini") -> dict[str, float]:
    """{source: usd} for today's (UTC) locally-ingested usage rows."""
    init()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(today.timestamp())
    with _conn() as c:
        rows = c.execute(
            """
            SELECT COALESCE(NULLIF(source, ''), 'untagged') AS src, model,
                   COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
            FROM usage_reports
            WHERE provider = ? AND ts >= ?
            GROUP BY src, model
            """,
            (provider, start),
        ).fetchall()
    out: dict[str, float] = {}
    for src, model, in_tok, out_tok in rows:
        in_price, out_price = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICE)
        out[src] = out.get(src, 0.0) + (in_tok * in_price + out_tok * out_price) / 1_000_000
    return out


def read_attribution(days: int = 14, tz=None) -> dict[str, Any]:
    """Per-source local-day spend for every attributed source seen in the
    window. Sources are snapshot providers containing ':' — e.g.
    'anthropic:Recorderbot-key', 'openai:Shell', 'gemini:recorderbot'."""
    init()
    tzi = _tzinfo(tz)
    first_ts = int(
        (datetime.now(tzi) - timedelta(days=days - 1))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    with _conn() as c:
        srcs = [
            row[0]
            for row in c.execute(
                "SELECT DISTINCT provider FROM snapshots "
                "WHERE ts >= ? AND provider LIKE '%:%' ORDER BY provider",
                (first_ts,),
            )
        ]
    sources = []
    for src in srcs:
        series = daily_spend_series(src, days, tz=tzi)
        total = round(sum(v for _d, v in series), 4)
        sources.append({
            "source": src,
            "today": series[-1][1],
            "total": total,
            "series": [{"date": d, "usd": v} for d, v in series],
        })
    sources.sort(key=lambda s: -s["total"])
    return {"days": days, "sources": sources}


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
