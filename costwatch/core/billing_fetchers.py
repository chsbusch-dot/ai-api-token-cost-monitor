"""External cost fetchers. All async, all degrade gracefully on auth/scope errors."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from .billing import BillingTracker, PRICING_USD_PER_MTOK, DEFAULT_PRICE

log = logging.getLogger(__name__)


# ── Deepgram (prepaid balance) ───────────────────────────────────────────────

async def fetch_deepgram_balance(api_key: str, tracker: BillingTracker) -> None:
    """Read remaining USD balance from the user's first Deepgram project.

    Requires the key to have `billing:read` scope (Member or Owner role, not
    the default listen-only key). On 403 we record the error once and stop
    trying — the user fixes it by swapping in a properly-scoped key.
    """
    if tracker.state.deepgram_error is not None:
        return  # already determined this key can't read balance — don't keep trying

    if not api_key:
        return

    headers = {"Authorization": f"Token {api_key}"}
    timeout = httpx.Timeout(8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get("https://api.deepgram.com/v1/projects", headers=headers)
            if r.status_code == 401:
                tracker.set_deepgram_error("auth failed (check DEEPGRAM_ADMIN_API_KEY)")
                return
            r.raise_for_status()
            projects = r.json().get("projects", [])
            if not projects:
                tracker.set_deepgram_error("no projects")
                return
            pid = projects[0].get("project_id")
            if not pid:
                tracker.set_deepgram_error("project_id missing")
                return

            r = await client.get(
                f"https://api.deepgram.com/v1/projects/{pid}/balances",
                headers=headers,
            )
            if r.status_code == 403:
                tracker.set_deepgram_error(
                    "Key lacks billing:read scope. "
                    "Fix: console.deepgram.com → API Keys → create new key with "
                    "Member or Owner role, then update DEEPGRAM_ADMIN_API_KEY."
                )
                return
            r.raise_for_status()
            balances = r.json().get("balances", [])
            total = sum(
                float(b.get("amount", 0) or 0)
                for b in balances
                if (b.get("units") or "usd").lower() == "usd"
            )
            tracker.set_deepgram_balance(total)
    except httpx.HTTPError as e:
        log.debug("deepgram balance fetch failed: %s", e)
        # Don't set a permanent error for transient network failures.


# ── Anthropic prepaid credit balance (browser session cookie required) ─────

def _parse_balance_usd(data) -> Optional[float]:
    """Extract a USD balance from the Anthropic console response.

    Real shape (verified 2026-05):
        {"amount": 672, "currency": "USD",
         "auto_reload_settings": {...}, ...}

    `amount` is in MINOR units (cents for USD). The fallback paths handle
    other shapes in case Anthropic changes the API.
    """
    if not isinstance(data, dict):
        return None

    # Anthropic console shape: amount + currency, value in minor units.
    if "amount" in data and isinstance(data["amount"], (int, float)):
        amount = float(data["amount"])
        currency = (data.get("currency") or "USD").upper()
        if currency == "USD":
            return amount / 100.0  # cents → dollars
        # Non-USD: best-effort. Caller can interpret based on dashboard label.
        return amount / 100.0

    # Fallback shapes — kept defensively in case the API changes.
    def _coerce(v) -> Optional[float]:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.replace("$", "").replace(",", "").strip())
            except ValueError:
                return None
        if isinstance(v, dict):
            for k in ("amount", "value", "amount_usd"):
                if k in v:
                    return _coerce(v[k])
        return None

    flat_keys = (
        "balance", "balance_usd", "available_balance",
        "credit_balance", "credits_balance",
        "available_credits", "available_credits_usd",
        "remaining_credits", "remaining_credits_usd",
        "amount_usd", "total_amount",
    )
    for k in flat_keys:
        if k in data:
            v = _coerce(data[k])
            if v is not None:
                return v
    for nested_key in ("credits", "credit", "balance", "data", "result"):
        if isinstance(data.get(nested_key), dict):
            v = _parse_balance_usd(data[nested_key])
            if v is not None:
                return v
    return None


async def fetch_anthropic_balance(
    org_id: str, session_cookie: str, tracker: BillingTracker
) -> None:
    """Pull current Anthropic prepaid credit balance via the console's internal API.

    Requires a browser session cookie because the public/admin API doesn't expose
    this. Cookie expires; on 401/403 we set anthropic_balance_error once and stop
    until the daemon is restarted with a fresh cookie.
    """
    if tracker.state.anthropic_balance_error is not None:
        return
    if not org_id or not session_cookie:
        return

    url = f"https://platform.claude.com/api/organizations/{org_id}/prepaid/credits"
    headers = {
        "Cookie": session_cookie,
        "Accept": "application/json",
        "User-Agent": "costwatch/0.1",
    }
    timeout = httpx.Timeout(8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
            if r.status_code in (401, 403):
                tracker.state.anthropic_balance_error = (
                    "session cookie expired — refresh ANTHROPIC_SESSION_COOKIE"
                )
                tracker.state.anthropic_balance_usd = None
                return
            if r.status_code == 404:
                tracker.state.anthropic_balance_error = "endpoint not found (check ANTHROPIC_ORG_ID)"
                return
            r.raise_for_status()
            data = r.json()
            balance = _parse_balance_usd(data)
            if balance is None:
                # Unknown response shape — log the top-level keys (not values)
                # so we can adapt the parser.
                shape = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                log.info("Anthropic balance: could not parse response (top-level keys=%s)", shape)
                tracker.state.anthropic_balance_error = "response shape not recognized"
                return
            tracker.state.anthropic_balance_usd = balance
            tracker.state.anthropic_balance_error = None
    except httpx.HTTPError as e:
        log.debug("anthropic balance fetch failed: %s", e)
        # Don't permanently disable on transient errors.


# ── Anthropic Cost API (admin key required) ─────────────────────────────────

async def fetch_anthropic_today_cost(admin_api_key: str, tracker: BillingTracker) -> Optional[float]:
    """Estimate today's (UTC) Anthropic spend via the Usage Report API.

    The Cost Report API excludes the current day, so we use the Usage Report
    (which supports 1h granularity and live data with ~5min freshness), then
    multiply token counts by our local pricing table. Less accurate than the
    Cost API for past days (no cache/batch discount adjustments), but gives a
    live current-day signal.

    Requires:
      - An Admin API key (`sk-ant-admin...`)
      - Account is part of an organization (individual accounts can't use this)
    """
    if getattr(tracker.state, "_anthropic_admin_disabled", False):
        return None

    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ending = now.replace(second=0, microsecond=0)
    if ending <= today:
        return None  # right at midnight — wait a minute
    params = [
        ("starting_at", today.isoformat().replace("+00:00", "Z")),
        ("ending_at", ending.isoformat().replace("+00:00", "Z")),
        ("bucket_width", "1h"),
        ("group_by[]", "model"),
    ]
    headers = {
        "anthropic-version": "2023-06-01",
        "x-api-key": admin_api_key,
        "User-Agent": "costwatch/0.1",
    }
    timeout = httpx.Timeout(8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/organizations/usage_report/messages",
                params=params,
                headers=headers,
            )
            if r.status_code in (400, 401, 403, 404):
                body = r.text[:300] if r.text else "(no body)"
                log.info(
                    "Anthropic Cost API unavailable (status=%d) — disabling. body=%s",
                    r.status_code, body,
                )
                setattr(tracker.state, "_anthropic_admin_disabled", True)
                return None
            r.raise_for_status()
            data = r.json()
            return _compute_today_cost_from_usage(data)
    except httpx.HTTPError as e:
        log.debug("anthropic usage fetch failed: %s", e)
        return None


# Cache writes carry a premium and reads are cheap — multipliers on the model's input price.
# Source: Anthropic prompt-caching docs. 5m cache write ≈ 1.25× input, 1h ≈ 2×, reads ≈ 0.1×.
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
CACHE_READ_MULT = 0.1
WEB_SEARCH_USD_PER_REQUEST = 0.01


def _compute_today_cost_from_usage(data: dict) -> float:
    """Sum per-model token usage across all buckets and apply pricing locally."""
    total_usd = 0.0
    for bucket in data.get("data", []):
        for r in bucket.get("results", []):
            model = r.get("model") or ""
            in_price, out_price = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICE)

            uncached_in = r.get("uncached_input_tokens") or 0
            cache_read = r.get("cache_read_input_tokens") or 0
            cache_create = r.get("cache_creation") or {}
            cache_5m = cache_create.get("ephemeral_5m_input_tokens") or 0
            cache_1h = cache_create.get("ephemeral_1h_input_tokens") or 0
            output = r.get("output_tokens") or 0
            web_search = (r.get("server_tool_use") or {}).get("web_search_requests") or 0

            input_cost = (
                uncached_in
                + cache_read * CACHE_READ_MULT
                + cache_5m * CACHE_WRITE_5M_MULT
                + cache_1h * CACHE_WRITE_1H_MULT
            ) * in_price / 1_000_000
            output_cost = output * out_price / 1_000_000
            tool_cost = web_search * WEB_SEARCH_USD_PER_REQUEST

            total_usd += input_cost + output_cost + tool_cost
    return total_usd


# ── OpenAI Cost API (admin key required) ────────────────────────────────────

async def fetch_openai_today_cost(admin_api_key: str, tracker: BillingTracker) -> Optional[float]:
    """Pull total USD cost for the current UTC day via OpenAI's organization costs endpoint.

    Requires:
      - An Admin API key (`sk-admin-...`), generated at platform.openai.com →
        Organization → Admin keys (org owner only).
      - Account is part of an organization.
    """
    if getattr(tracker.state, "_openai_admin_disabled", False):
        return None

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(today.timestamp())
    end = int((today + timedelta(days=1)).timestamp())
    url = (
        f"https://api.openai.com/v1/organization/costs"
        f"?start_time={start}&end_time={end}&bucket_width=1d&limit=31"
    )
    headers = {
        "Authorization": f"Bearer {admin_api_key}",
        "User-Agent": "costwatch/0.1",
    }
    timeout = httpx.Timeout(8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
            if r.status_code in (401, 403, 404):
                log.info(
                    "OpenAI Cost API unavailable (status=%d) — disabling.",
                    r.status_code,
                )
                setattr(tracker.state, "_openai_admin_disabled", True)
                return None
            r.raise_for_status()
            data = r.json()
            total_usd = 0.0
            for bucket in data.get("data", []):
                for result in bucket.get("results", []):
                    amount = result.get("amount") or {}
                    val = amount.get("value")
                    if isinstance(val, (int, float)):
                        total_usd += float(val)
            return total_usd
    except httpx.HTTPError as e:
        log.debug("openai cost fetch failed: %s", e)
        return None


# ── Polling loop ─────────────────────────────────────────────────────────────

async def billing_poller(
    tracker: BillingTracker,
    anthropic_admin_key: Optional[str],
    openai_admin_key: Optional[str],
    deepgram_api_key: Optional[str],
    on_update,
    interval_seconds: int = 60,
    stop: Optional[asyncio.Event] = None,
    include_gemini: bool = True,
    anthropic_org_id: Optional[str] = None,
    anthropic_session_cookie: Optional[str] = None,
):
    """Periodically refresh provider day-totals and call `on_update(state)`."""

    # Lazy import — avoids forcing SQLite init at module import time.
    from ..store import today_usd_by_provider

    async def tick():
        coros = []
        if anthropic_admin_key:
            async def _anth():
                today = await fetch_anthropic_today_cost(anthropic_admin_key, tracker)
                tracker.state.anthropic_today_usd = today
            coros.append(_anth())
        if anthropic_org_id and anthropic_session_cookie:
            coros.append(fetch_anthropic_balance(anthropic_org_id, anthropic_session_cookie, tracker))
        if openai_admin_key:
            async def _oai():
                today = await fetch_openai_today_cost(openai_admin_key, tracker)
                tracker.state.openai_today_usd = today
            coros.append(_oai())
        if deepgram_api_key:
            coros.append(fetch_deepgram_balance(deepgram_api_key, tracker))
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        if include_gemini:
            # Synchronous SQLite read — fast (single indexed query). Run in default
            # executor to keep the loop unblocked under load.
            loop = asyncio.get_running_loop()
            tracker.state.gemini_today_usd = await loop.run_in_executor(
                None, today_usd_by_provider, "gemini"
            )
        try:
            await on_update(tracker.state)
        except Exception:
            log.debug("on_update failed", exc_info=True)

    await tick()
    while True:
        try:
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        await tick()


# ── Smoke test (step 1 deliverable) ─────────────────────────────────────────
# Run: `python -m costwatch.core.billing_fetchers`
# Reads ANTHROPIC_ADMIN_API_KEY and OPENAI_ADMIN_API_KEY from the environment.

if __name__ == "__main__":
    import os
    import sys

    # Best-effort .env load — optional dependency
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    anth_key = os.getenv("ANTHROPIC_ADMIN_API_KEY")
    oai_key = os.getenv("OPENAI_ADMIN_API_KEY")
    dg_key = os.getenv("DEEPGRAM_ADMIN_API_KEY")

    async def main():
        tracker = BillingTracker()
        tasks = []
        labels = []
        if anth_key:
            async def _anth():
                v = await fetch_anthropic_today_cost(anth_key, tracker)
                tracker.state.anthropic_today_usd = v
                return v
            tasks.append(_anth()); labels.append("anthropic_spend")
        if oai_key:
            async def _oai():
                v = await fetch_openai_today_cost(oai_key, tracker)
                tracker.state.openai_today_usd = v
                return v
            tasks.append(_oai()); labels.append("openai_spend")
        if dg_key:
            tasks.append(fetch_deepgram_balance(dg_key, tracker))
            labels.append("deepgram_balance")

        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

        for label, r in zip(labels, results):
            if label == "anthropic_spend":
                if isinstance(r, Exception):
                    print(f"anthropic: error: {r}")
                elif r is None:
                    print("anthropic: unavailable (auth scope or non-org account?)")
                else:
                    print(f"anthropic today: ${r:.4f}")
            elif label == "openai_spend":
                if isinstance(r, Exception):
                    print(f"openai: error: {r}")
                elif r is None:
                    print("openai: unavailable (auth scope or non-org account?)")
                else:
                    print(f"openai today: ${r:.4f}")
            elif label == "deepgram_balance":
                if isinstance(r, Exception):
                    print(f"deepgram: error: {r}")
                elif tracker.state.deepgram_error:
                    print(f"deepgram: {tracker.state.deepgram_error}")
                elif tracker.state.deepgram_balance_usd is None:
                    print("deepgram: unavailable")
                else:
                    print(f"deepgram balance: ${tracker.state.deepgram_balance_usd:.4f} remaining")

        # Gemini: local SQLite usage_reports rolled up × pricing
        from ..store import today_usd_by_provider
        gemini_today = today_usd_by_provider("gemini")
        tracker.state.gemini_today_usd = gemini_today
        print(f"gemini today: ${gemini_today:.4f}  (local token tracking; record via `python -m costwatch.ingest gemini ...`)")

        total = tracker.state.total_today_usd()
        if total is not None:
            print("────")
            print(f"total spent today: ${total:.4f}")

    asyncio.run(main())
