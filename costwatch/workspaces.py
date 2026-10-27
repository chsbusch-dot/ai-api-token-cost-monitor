"""Anthropic usage per Console workspace, for the daily digest.

Reads the Admin API usage report grouped by workspace and model and prices each row with the
same table the live tracker uses (`_usage_row_cost_usd`). That is list price: usage paid from
the Max plan's monthly API credits shows as gross spend, which is what the credit balance is
drawn down by. Workspace names come from the workspaces list; the usage report names the
org's default workspace `null`.

Two windows per digest: "today" (local midnight to now, hourly buckets) and the credit cycle to
date (from the last ANTHROPIC_CREDIT_CYCLE_DAY, daily UTC buckets). ANTHROPIC_MONTHLY_CREDITS_USD
(e.g. 200) adds a used-of-credits line. Sync httpx: it runs once a day, not on the poll tick.
Returns None when no Admin key is set or the API cannot be read, so the digest still goes out.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .core.billing_fetchers import _usage_row_cost_usd

log = logging.getLogger(__name__)

API = "https://api.anthropic.com/v1/organizations"


def _get(client: httpx.Client, path: str, params: list[tuple[str, str]]) -> dict:
    r = client.get(f"{API}/{path}", params=params)
    if r.status_code != 200:
        # Status only: an httpx error string carries the URL and headers are never logged.
        raise RuntimeError(f"Anthropic Admin API {path}: HTTP {r.status_code}")
    return r.json()


def workspace_names(client: httpx.Client) -> dict[Optional[str], str]:
    """workspace id -> name; the default workspace is keyed None, as the usage report names it."""
    def listed(params: list[tuple[str, str]]) -> list[dict]:
        out: list[dict] = []
        after = None
        while True:
            page = _get(client, "workspaces",
                        params + [("limit", "1000")] + ([("after_id", after)] if after else []))
            out += page.get("data") or []
            if not page.get("has_more") or not page.get("last_id"):
                return out
            after = page["last_id"]

    named = {w["id"]: w.get("name") or w["id"] for w in listed([("include_archived", "true")])}
    names: dict[Optional[str], str] = dict(named)
    names[None] = "Default"
    return names


def usage_by_workspace(client: httpx.Client, start: datetime, end: datetime,
                       bucket: str) -> dict[Optional[str], dict[str, float]]:
    """workspace id -> {"usd", "tokens"} for usage in [start, end)."""
    out: dict[Optional[str], dict[str, float]] = {}
    if end <= start:
        return out
    params = [("starting_at", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
              ("ending_at", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
              ("bucket_width", bucket), ("limit", "168" if bucket == "1h" else "31"),
              ("group_by[]", "workspace_id"), ("group_by[]", "model")]
    page = None
    while True:
        rep = _get(client, "usage_report/messages", params + ([("page", page)] if page else []))
        for b in rep.get("data") or []:
            for r in b.get("results") or []:
                cc = r.get("cache_creation") or {}
                tokens = sum(int(r.get(k) or 0) for k in
                             ("uncached_input_tokens", "cache_read_input_tokens", "output_tokens")) \
                    + int(cc.get("ephemeral_5m_input_tokens") or 0) + int(cc.get("ephemeral_1h_input_tokens") or 0)
                if not tokens:
                    continue
                agg = out.setdefault(r.get("workspace_id"), {"usd": 0.0, "tokens": 0.0})
                agg["usd"] += _usage_row_cost_usd(r)
                agg["tokens"] += tokens
        if not rep.get("has_more") or not rep.get("next_page"):
            return out
        page = rep["next_page"]


def cycle_start(now: datetime, cycle_day: int) -> datetime:
    """Midnight UTC of the most recent `cycle_day` of the month at or before `now`."""
    day = max(1, min(28, cycle_day))
    start = now.astimezone(timezone.utc).replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if start > now.astimezone(timezone.utc):
        prev = (start.replace(day=1) - timedelta(days=1)).replace(day=day)
        start = prev
    return start


def build(now: datetime, admin_key: Optional[str] = None,
          client: Optional[httpx.Client] = None) -> Optional[dict[str, Any]]:
    """The digest's by-workspace section, or None (no key, or the API could not be read)."""
    key = admin_key if admin_key is not None else os.getenv("ANTHROPIC_ADMIN_API_KEY", "")
    if not key:
        return None
    cycle_day = int(os.getenv("ANTHROPIC_CREDIT_CYCLE_DAY", "1") or 1)
    credits = float(os.getenv("ANTHROPIC_MONTHLY_CREDITS_USD", "0") or 0)
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_now = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    start = cycle_start(now, cycle_day)
    own = client is None
    client = client or httpx.Client(timeout=20.0, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "costwatch/0.1"})
    try:
        names = workspace_names(client)
        today = usage_by_workspace(client, local_midnight.astimezone(timezone.utc), utc_now, "1h")
        cycle = usage_by_workspace(client, start, utc_now, "1d")
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        log.warning("by-workspace digest section skipped: %s", e)
        return None
    finally:
        if own:
            client.close()
    rows = []
    for wid in set(today) | set(cycle):
        rows.append({
            "workspace": names.get(wid, wid or "Default"),
            "today_usd": round(today.get(wid, {}).get("usd", 0.0), 4),
            "today_tokens": int(today.get(wid, {}).get("tokens", 0)),
            "cycle_usd": round(cycle.get(wid, {}).get("usd", 0.0), 4),
        })
    rows.sort(key=lambda r: (-r["cycle_usd"], -r["today_usd"], r["workspace"].lower()))
    cycle_total = round(sum(r["cycle_usd"] for r in rows), 2)
    return {
        "rows": rows,
        "today_total": round(sum(r["today_usd"] for r in rows), 4),
        "cycle_start": start.strftime("%Y-%m-%d"),
        "cycle_total": cycle_total,
        "credits_usd": credits or None,
    }
