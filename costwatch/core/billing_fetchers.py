"""External cost fetchers. All async, all degrade gracefully on auth/scope errors.

Failure policy (uniform across fetchers):
  - Transient errors (network, 5xx) → return None; caller keeps last value.
  - 429 → short cooldown (RATE_LIMIT_COOLDOWN_S) before the next attempt.
  - Auth/scope/config errors (400/401/403/404) → hourly retry cooldown, NOT a
    permanent disable — a long-running daemon must survive vendor incidents
    and key rotations without a restart.
Cooldowns are stored as `_<name>_retry_at` epoch attrs on the tracker state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from .billing import BillingTracker, PRICING_USD_PER_MTOK, DEFAULT_PRICE

log = logging.getLogger(__name__)

RATE_LIMIT_COOLDOWN_S = 300   # after a 429
AUTH_RETRY_COOLDOWN_S = 3600  # after 400/401/403/404


def _in_cooldown(state, attr: str) -> bool:
    return time.time() < (getattr(state, attr, 0) or 0)


def _set_cooldown(state, attr: str, seconds: float) -> None:
    setattr(state, attr, time.time() + seconds)


# ── Deepgram (prepaid balance) ───────────────────────────────────────────────

async def fetch_deepgram_balance(api_key: str, tracker: BillingTracker) -> None:
    """Read remaining USD balance from the user's first Deepgram project.

    Requires the key to have `billing:read` scope (Member or Owner role, not
    the default listen-only key). Auth/scope failures set a user-visible error
    and retry hourly.
    """
    if _in_cooldown(tracker.state, "_deepgram_retry_at"):
        return

    if not api_key:
        return

    headers = {"Authorization": f"Token {api_key}"}
    timeout = httpx.Timeout(8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get("https://api.deepgram.com/v1/projects", headers=headers)
            if r.status_code == 401:
                tracker.set_deepgram_error("auth failed (check DEEPGRAM_ADMIN_API_KEY)")
                _set_cooldown(tracker.state, "_deepgram_retry_at", AUTH_RETRY_COOLDOWN_S)
                return
            if r.status_code == 429:
                _set_cooldown(tracker.state, "_deepgram_retry_at", RATE_LIMIT_COOLDOWN_S)
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
                _set_cooldown(tracker.state, "_deepgram_retry_at", AUTH_RETRY_COOLDOWN_S)
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
    this. Cookie expires; on 401/403 we set anthropic_balance_error and retry
    hourly (the error clears automatically once a working cookie is in place).
    """
    if _in_cooldown(tracker.state, "_anth_balance_retry_at"):
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
                _set_cooldown(tracker.state, "_anth_balance_retry_at", AUTH_RETRY_COOLDOWN_S)
                return
            if r.status_code == 404:
                tracker.state.anthropic_balance_error = "endpoint not found (check ANTHROPIC_ORG_ID)"
                _set_cooldown(tracker.state, "_anth_balance_retry_at", AUTH_RETRY_COOLDOWN_S)
                return
            if r.status_code == 429:
                _set_cooldown(tracker.state, "_anth_balance_retry_at", RATE_LIMIT_COOLDOWN_S)
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

async def fetch_anthropic_today_cost(
    admin_api_key: str, tracker: BillingTracker
) -> Optional[tuple[float, dict[Optional[str], float]]]:
    """Estimate today's (UTC) Anthropic spend via the Usage Report API.

    Returns (total_usd, usd_by_api_key_id) — one request grouped by
    model + api_key_id serves both the headline total and per-key attribution
    without extra rate-limit cost. None on failure/cooldown.

    We use the Usage Report (1h granularity, ~5min freshness) and multiply
    token counts by our local pricing table. Less accurate than the Cost
    Report for past days (no batch/discount adjustments), but live — and it
    is the only surface that attributes to individual API keys (the Cost
    Report only groups by workspace).

    Requires:
      - An Admin API key (`sk-ant-admin...`)
      - Account is part of an organization (individual accounts can't use this)
    """
    if _in_cooldown(tracker.state, "_anthropic_usage_retry_at"):
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
        ("group_by[]", "api_key_id"),
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
            if r.status_code == 429:
                log.info("Anthropic usage API rate-limited (429) — cooling down %ds", RATE_LIMIT_COOLDOWN_S)
                _set_cooldown(tracker.state, "_anthropic_usage_retry_at", RATE_LIMIT_COOLDOWN_S)
                return None
            if r.status_code in (400, 401, 403, 404):
                body = r.text[:300] if r.text else "(no body)"
                log.info(
                    "Anthropic usage API unavailable (status=%d) — retrying in %ds. body=%s",
                    r.status_code, AUTH_RETRY_COOLDOWN_S, body,
                )
                _set_cooldown(tracker.state, "_anthropic_usage_retry_at", AUTH_RETRY_COOLDOWN_S)
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


def _usage_row_cost_usd(r: dict) -> float:
    """Estimated USD cost of one usage-report result row (tokens × pricing)."""
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
    return input_cost + output_cost + tool_cost


def _compute_today_cost_from_usage(data: dict) -> tuple[float, dict[Optional[str], float]]:
    """Sum usage across all buckets → (total_usd, usd_by_api_key_id).

    Rows carry api_key_id when the request was grouped by it; rows without one
    (Workbench/Console traffic) accumulate under key None.
    """
    total_usd = 0.0
    by_key: dict[Optional[str], float] = {}
    for bucket in data.get("data", []):
        for r in bucket.get("results", []):
            usd = _usage_row_cost_usd(r)
            total_usd += usd
            kid = r.get("api_key_id")
            by_key[kid] = by_key.get(kid, 0.0) + usd
    return total_usd, by_key


# ── Anthropic API key name resolution (for attribution labels) ──────────────

KEY_NAME_CACHE_TTL_S = 3600


async def get_anthropic_key_names(admin_api_key: str, tracker: BillingTracker) -> dict[str, str]:
    """{api_key_id: key_name} for the org, cached in-memory for 1h.
    Returns the stale cache (or {}) on fetch failure."""
    cache = getattr(tracker.state, "_anth_key_names", None)
    cached_at = getattr(tracker.state, "_anth_key_names_at", 0) or 0
    if cache is not None and time.time() - cached_at < KEY_NAME_CACHE_TTL_S:
        return cache

    headers = {
        "anthropic-version": "2023-06-01",
        "x-api-key": admin_api_key,
        "User-Agent": "costwatch/0.1",
    }
    mapping: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            page: Optional[str] = None
            for _ in range(10):
                params: list[tuple[str, str]] = [("limit", "100")]
                if page:
                    params.append(("after_id", page))
                r = await client.get(
                    "https://api.anthropic.com/v1/organizations/api_keys",
                    params=params, headers=headers,
                )
                if r.status_code != 200:
                    return cache or {}
                body = r.json()
                for k in body.get("data", []):
                    if k.get("id"):
                        mapping[k["id"]] = k.get("name") or k["id"]
                if not body.get("has_more"):
                    break
                page = body.get("last_id")
                if not page:
                    break
        setattr(tracker.state, "_anth_key_names", mapping)
        setattr(tracker.state, "_anth_key_names_at", time.time())
        return mapping
    except httpx.HTTPError:
        return cache or {}


# ── Anthropic Claude Code Analytics API (admin key required; added 2026) ────

CLAUDE_CODE_POLL_INTERVAL_S = 900  # data is daily-aggregated with ~1h delay — no point polling faster


def _sum_claude_code_costs(pages: list[dict]) -> float:
    """Sum estimated_cost across all actors/models in a day's report pages.

    Response shape (docs, verified 2026-07): data[].model_breakdown[].estimated_cost
    = {"amount": <minor units, e.g. cents>, "currency": "USD"}.
    """
    total_minor = 0.0
    for page in pages:
        for record in page.get("data", []):
            for mb in record.get("model_breakdown", []) or []:
                cost = mb.get("estimated_cost") or {}
                amount = cost.get("amount")
                if isinstance(amount, (int, float)) and (cost.get("currency") or "USD").upper() == "USD":
                    total_minor += float(amount)
    return total_minor / 100.0


async def fetch_claude_code_today_cost(admin_api_key: str, tracker: BillingTracker) -> Optional[float]:
    """Today's (UTC) org-billed Claude Code spend via the Claude Code Analytics API.

    GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD
    Covers Claude Code usage attributed to the org (both api and subscription
    customer types per the schema). Daily granularity, ~1h data delay — polled
    at most every CLAUDE_CODE_POLL_INTERVAL_S regardless of tick cadence.
    """
    if _in_cooldown(tracker.state, "_claude_code_retry_at"):
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {
        "anthropic-version": "2023-06-01",
        "x-api-key": admin_api_key,
        "User-Agent": "costwatch/0.1",
    }
    timeout = httpx.Timeout(10.0)
    pages: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            page_token: Optional[str] = None
            for _ in range(10):  # pagination safety bound
                params: list[tuple[str, str]] = [("starting_at", today), ("limit", "1000")]
                if page_token:
                    params.append(("page", page_token))
                r = await client.get(
                    "https://api.anthropic.com/v1/organizations/usage_report/claude_code",
                    params=params,
                    headers=headers,
                )
                if r.status_code == 429:
                    _set_cooldown(tracker.state, "_claude_code_retry_at", RATE_LIMIT_COOLDOWN_S)
                    return None
                if r.status_code in (400, 401, 403, 404):
                    log.info(
                        "Claude Code Analytics API unavailable (status=%d) — retrying in %ds",
                        r.status_code, AUTH_RETRY_COOLDOWN_S,
                    )
                    _set_cooldown(tracker.state, "_claude_code_retry_at", AUTH_RETRY_COOLDOWN_S)
                    return None
                r.raise_for_status()
                body = r.json()
                pages.append(body)
                if not body.get("has_more"):
                    break
                page_token = body.get("next_page")
                if not page_token:
                    break
        _set_cooldown(tracker.state, "_claude_code_retry_at", CLAUDE_CODE_POLL_INTERVAL_S)
        return _sum_claude_code_costs(pages)
    except httpx.HTTPError as e:
        log.debug("claude code usage fetch failed: %s", e)
        return None


# ── OpenAI Cost API (admin key required) ────────────────────────────────────

def _sum_openai_costs(data: dict) -> tuple[float, dict[Optional[str], float]]:
    """(total_usd, usd_by_project_id) from a costs response grouped by project_id."""
    total_usd = 0.0
    by_project: dict[Optional[str], float] = {}
    for bucket in data.get("data", []):
        for result in bucket.get("results", []):
            amount = result.get("amount") or {}
            val = amount.get("value")
            if isinstance(val, (int, float)):
                total_usd += float(val)
                pid = result.get("project_id")
                by_project[pid] = by_project.get(pid, 0.0) + float(val)
    return total_usd, by_project


async def fetch_openai_today_cost(
    admin_api_key: str, tracker: BillingTracker
) -> Optional[tuple[float, dict[Optional[str], float]]]:
    """Pull today's (UTC) USD cost via OpenAI's organization costs endpoint,
    grouped by project — one request serves both the total (sum) and
    per-project attribution.

    Requires:
      - An Admin API key (`sk-admin-...`), generated at platform.openai.com →
        Organization → Admin keys (org owner only).
      - Account is part of an organization.
    """
    if _in_cooldown(tracker.state, "_openai_cost_retry_at"):
        return None

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(today.timestamp())
    end = int((today + timedelta(days=1)).timestamp())
    url = (
        f"https://api.openai.com/v1/organization/costs"
        f"?start_time={start}&end_time={end}&bucket_width=1d&limit=31"
        f"&group_by=project_id"
    )
    headers = {
        "Authorization": f"Bearer {admin_api_key}",
        "User-Agent": "costwatch/0.1",
    }
    timeout = httpx.Timeout(8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 429:
                log.info("OpenAI cost API rate-limited (429) — cooling down %ds", RATE_LIMIT_COOLDOWN_S)
                _set_cooldown(tracker.state, "_openai_cost_retry_at", RATE_LIMIT_COOLDOWN_S)
                return None
            if r.status_code in (401, 403, 404):
                log.info(
                    "OpenAI Cost API unavailable (status=%d) — retrying in %ds.",
                    r.status_code, AUTH_RETRY_COOLDOWN_S,
                )
                _set_cooldown(tracker.state, "_openai_cost_retry_at", AUTH_RETRY_COOLDOWN_S)
                return None
            r.raise_for_status()
            return _sum_openai_costs(r.json())
    except httpx.HTTPError as e:
        log.debug("openai cost fetch failed: %s", e)
        return None


async def get_openai_project_names(admin_api_key: str, tracker: BillingTracker) -> dict[str, str]:
    """{project_id: name} for the org, cached in-memory for 1h.
    Returns the stale cache (or {}) on fetch failure."""
    cache = getattr(tracker.state, "_openai_proj_names", None)
    cached_at = getattr(tracker.state, "_openai_proj_names_at", 0) or 0
    if cache is not None and time.time() - cached_at < KEY_NAME_CACHE_TTL_S:
        return cache

    headers = {"Authorization": f"Bearer {admin_api_key}", "User-Agent": "costwatch/0.1"}
    mapping: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            after: Optional[str] = None
            for _ in range(10):
                params: list[tuple[str, str]] = [("limit", "100")]
                if after:
                    params.append(("after", after))
                r = await client.get(
                    "https://api.openai.com/v1/organization/projects",
                    params=params, headers=headers,
                )
                if r.status_code != 200:
                    return cache or {}
                body = r.json()
                for p in body.get("data", []):
                    if p.get("id"):
                        mapping[p["id"]] = p.get("name") or p["id"]
                if not body.get("has_more"):
                    break
                after = body.get("last_id")
                if not after:
                    break
        setattr(tracker.state, "_openai_proj_names", mapping)
        setattr(tracker.state, "_openai_proj_names_at", time.time())
        return mapping
    except httpx.HTTPError:
        return cache or {}


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
    from ..store import today_usd_by_gemini_source, today_usd_by_provider

    async def tick():
        # Values actually fetched THIS tick. write_snapshot persists only these,
        # so a failed fetch produces a snapshot gap (harmless to delta-sum
        # accounting) instead of a stale counter row. The display fields on
        # state keep their last-known value across transient failures.
        fresh: dict[str, float] = {}
        fresh_sources: dict[str, float] = {}
        coros = []
        if anthropic_admin_key:
            async def _anth():
                res = await fetch_anthropic_today_cost(anthropic_admin_key, tracker)
                if res is not None:
                    total, by_key = res
                    tracker.state.anthropic_today_usd = total
                    fresh["anthropic"] = total
                    names = await get_anthropic_key_names(anthropic_admin_key, tracker)
                    for kid, usd in by_key.items():
                        label = names.get(kid) or (kid if kid else "console")
                        fresh_sources[f"anthropic:{label}"] = (
                            fresh_sources.get(f"anthropic:{label}", 0.0) + usd
                        )
            coros.append(_anth())

            async def _cc():
                today = await fetch_claude_code_today_cost(anthropic_admin_key, tracker)
                if today is not None:
                    tracker.state.claude_code_today_usd = today
                    fresh["claude_code"] = today
            coros.append(_cc())
        if anthropic_org_id and anthropic_session_cookie:
            coros.append(fetch_anthropic_balance(anthropic_org_id, anthropic_session_cookie, tracker))
        if openai_admin_key:
            async def _oai():
                res = await fetch_openai_today_cost(openai_admin_key, tracker)
                if res is not None:
                    total, by_project = res
                    tracker.state.openai_today_usd = total
                    fresh["openai"] = total
                    names = await get_openai_project_names(openai_admin_key, tracker)
                    for pid, usd in by_project.items():
                        label = names.get(pid) or (pid if pid else "default")
                        fresh_sources[f"openai:{label}"] = (
                            fresh_sources.get(f"openai:{label}", 0.0) + usd
                        )
            coros.append(_oai())
        if deepgram_api_key:
            coros.append(fetch_deepgram_balance(deepgram_api_key, tracker))
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        if include_gemini:
            # Synchronous SQLite read — fast (single indexed query). Run in default
            # executor to keep the loop unblocked under load.
            try:
                loop = asyncio.get_running_loop()
                gem = await loop.run_in_executor(None, today_usd_by_provider, "gemini")
                tracker.state.gemini_today_usd = gem
                fresh["gemini"] = gem
                by_source = await loop.run_in_executor(None, today_usd_by_gemini_source)
                for src, usd in by_source.items():
                    fresh_sources[f"gemini:{src}"] = usd
            except Exception:
                log.exception("gemini local rollup failed")
        tracker.state.fresh_today = fresh
        tracker.state.fresh_sources = fresh_sources
        try:
            await on_update(tracker.state)
        except Exception:
            log.exception("on_update failed")

    async def guarded_tick():
        # The poller must outlive any single bad tick — an unhandled exception
        # here would silently kill the polling task while the web server keeps
        # serving stale data.
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll tick failed")

    await guarded_tick()
    while True:
        try:
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        await guarded_tick()


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
                res = await fetch_anthropic_today_cost(anth_key, tracker)
                if res is not None:
                    tracker.state.anthropic_today_usd = res[0]
                    return res[0]
                return None
            tasks.append(_anth()); labels.append("anthropic_spend")
        if oai_key:
            async def _oai():
                res = await fetch_openai_today_cost(oai_key, tracker)
                if res is not None:
                    tracker.state.openai_today_usd = res[0]
                    return res[0]
                return None
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
