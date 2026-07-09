"""Regression tests for local-calendar-day spend accounting.

Root-cause bug these guard against: the digest timer fires 18:00 local
(America/Los_Angeles). 18:00 PDT == 01:00 UTC of the NEXT day. The old digest
computed "today" as the UTC date, so every email reported a UTC day that was
one hour old — always ~$0, even on days with double-digit real spend.

The fix reconstructs spend for arbitrary windows (local days) by summing
deltas between consecutive snapshots of the vendors' cumulative UTC-day
counters, treating a UTC-day boundary as a counter reset.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from costwatch import store
from costwatch.digest import build_digest_payload
from costwatch.store import daily_spend_series, local_today_spend, spend_between

LA = ZoneInfo("America/Los_Angeles")


def utc_ts(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def insert_snapshots(provider: str, rows: list[tuple[int, float]]) -> None:
    store.init()
    with store._conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO snapshots (ts, provider, today_usd, balance_usd, by_model) "
            "VALUES (?, ?, ?, NULL, NULL)",
            [(ts, provider, val) for ts, val in rows],
        )


# ── spend_between: delta-sum mechanics ──────────────────────────────────────


def test_intra_day_deltas_summed():
    """Counter 0.10 → 0.30 → 0.50 within one UTC day; window starts after the
    anchor row, so it captures exactly the 0.40 increase."""
    d = (2026, 7, 6)
    insert_snapshots("anthropic", [
        (utc_ts(*d, 10, 0), 0.10),   # anchor, before window
        (utc_ts(*d, 11, 0), 0.30),
        (utc_ts(*d, 12, 0), 0.50),
    ])
    got = spend_between("anthropic", utc_ts(*d, 10, 30), utc_ts(*d, 13, 0),
                        now_ts=utc_ts(*d, 13, 0))
    assert abs(got - 0.40) < 1e-9


def test_utc_midnight_reset_not_double_counted():
    """Across UTC midnight the counter resets; the first new-day snapshot's
    value is the new-day spend, not a delta against yesterday's total."""
    insert_snapshots("anthropic", [
        (utc_ts(2026, 7, 6, 23, 58), 5.00),   # anchor
        (utc_ts(2026, 7, 6, 23, 59), 5.20),   # +0.20 in window
        (utc_ts(2026, 7, 7, 0, 1), 0.02),     # reset: +0.02, NOT -5.18 or +0.02-5.20
    ])
    got = spend_between("anthropic", utc_ts(2026, 7, 6, 23, 59), utc_ts(2026, 7, 7, 0, 30),
                        now_ts=utc_ts(2026, 7, 7, 0, 30))
    assert abs(got - 0.22) < 1e-9


def test_gap_within_day_is_exact():
    """A snapshot gap (daemon down) within the same UTC day loses nothing:
    the delta across the gap equals the spend during it."""
    d = (2026, 7, 6)
    insert_snapshots("anthropic", [
        (utc_ts(*d, 8, 0), 1.00),
        (utc_ts(*d, 14, 0), 3.00),   # 6h gap
    ])
    got = spend_between("anthropic", utc_ts(*d, 7, 0), utc_ts(*d, 15, 0),
                        now_ts=utc_ts(*d, 15, 0))
    # first row has no anchor → its full counter (1.00) counts, then +2.00
    assert abs(got - 3.00) < 1e-9


def test_downward_revision_clamped_to_zero():
    """Anthropic's usage report can revise hour buckets slightly downward;
    a negative delta must clamp to 0, not subtract spend."""
    d = (2026, 7, 6)
    insert_snapshots("anthropic", [
        (utc_ts(*d, 9, 0), 1.00),    # anchor
        (utc_ts(*d, 10, 0), 0.90),   # revision down → 0
        (utc_ts(*d, 11, 0), 1.10),   # +0.20
    ])
    got = spend_between("anthropic", utc_ts(*d, 9, 30), utc_ts(*d, 12, 0),
                        now_ts=utc_ts(*d, 12, 0))
    assert abs(got - 0.20) < 1e-9


# ── local-day bucketing ─────────────────────────────────────────────────────


def test_spend_buckets_into_local_days_not_utc():
    """Spend at 23:30 PDT and 00:30 PDT (next local day) both land in UTC day
    July 7 (06:30Z and 07:30Z), but must bucket into DIFFERENT local days."""
    insert_snapshots("anthropic", [
        (utc_ts(2026, 7, 7, 6, 0), 1.00),    # 23:00 PDT Jul 6 (anchor value)
        (utc_ts(2026, 7, 7, 6, 30), 1.50),   # 23:30 PDT Jul 6 → local Jul 6
        (utc_ts(2026, 7, 7, 7, 30), 2.50),   # 00:30 PDT Jul 7 → local Jul 7
    ])
    now = utc_ts(2026, 7, 7, 8, 0)  # 01:00 PDT Jul 7
    series = dict(daily_spend_series("anthropic", 3, tz=LA, now_ts=now))
    assert abs(series["2026-07-06"] - 1.50) < 1e-9  # 1.00 (first) + 0.50
    assert abs(series["2026-07-07"] - 1.00) < 1e-9  # 2.50 - 1.50


def test_six_pm_pdt_digest_reports_full_local_day():
    """THE regression: at 18:00 PDT (= 01:00 UTC next day), the digest must
    report the full local day's spend, not the 1-hour-old UTC day (~$0)."""
    # Local day: Wednesday 2026-07-08 PDT. UTC day Jul 8 runs 07:00Z Jul 8 → 07:00Z Jul 9.
    insert_snapshots("anthropic", [
        (utc_ts(2026, 7, 8, 7, 10), 0.00),    # 00:10 PDT
        (utc_ts(2026, 7, 8, 16, 0), 4.20),    # 09:00 PDT — morning spend
        (utc_ts(2026, 7, 8, 23, 59), 9.75),   # 16:59 PDT — afternoon spend
        (utc_ts(2026, 7, 9, 0, 30), 0.05),    # 17:30 PDT — UTC day rolled, counter reset
        (utc_ts(2026, 7, 9, 0, 59), 0.10),    # 17:59 PDT
    ])
    now = utc_ts(2026, 7, 9, 1, 0)  # exactly 18:00 PDT Jul 8 — digest fire time
    payload = build_digest_payload(tz=LA, now_ts=now)

    assert payload["date"] == "2026-07-08", "digest must be dated with the LOCAL day"
    # Full local-day spend: 9.75 (UTC day Jul 8) + 0.10 (first hour of UTC day Jul 9)
    assert abs(payload["by_provider"]["anthropic"]["today"] - 9.85) < 1e-6, (
        f"expected $9.85 local-day spend, got {payload['by_provider']['anthropic']['today']} "
        "(the old UTC bug would report ~$0.10)"
    )


def test_digest_avg7_counts_empty_days_as_zero():
    """A quiet week must drag the 7-day average down — days without snapshots
    count as $0 instead of being skipped (which inflated the average)."""
    # Spend on exactly one of the prior 7 local days.
    insert_snapshots("anthropic", [
        (utc_ts(2026, 7, 3, 15, 0), 7.00),   # 08:00 PDT Jul 3
    ])
    now = utc_ts(2026, 7, 9, 1, 0)  # 18:00 PDT Jul 8
    payload = build_digest_payload(tz=LA, now_ts=now)
    assert abs(payload["by_provider"]["anthropic"]["avg7"] - 1.0) < 1e-6  # 7.00 / 7 days


def test_local_today_spend_zero_on_empty_db():
    assert local_today_spend("anthropic", tz=LA) == 0.0


# ── freshness-gated snapshot writes ─────────────────────────────────────────


def test_write_snapshot_skips_stale_display_values():
    """A failed fetch leaves fresh_today empty; write_snapshot must NOT persist
    the last-known display value (a stale counter carried across UTC midnight
    would be double-counted as new-day spend)."""
    from costwatch.core.billing import BillingState
    from costwatch.store import write_snapshot

    state = BillingState()
    state.anthropic_today_usd = 9.99   # last-known display value
    state.fresh_today = {}             # ...but this tick's fetch failed
    assert write_snapshot(state) == 0

    state.fresh_today = {"anthropic": 9.99}
    assert write_snapshot(state) == 1
