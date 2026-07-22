"""Build today's digest from snapshots + usage_reports.

All dates and "today" windows are the box's LOCAL calendar day — not UTC.
The vendor counters in the snapshots table reset at UTC midnight, so spend
per local day is reconstructed with delta-sum accounting (see
store.daily_spend_series). This matters because the digest timer fires at
18:00 local: in PDT that is 01:00 UTC of the NEXT day, and a UTC-based
"today" would always be ~1 hour old and report ~$0.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Optional

from .core.billing import PRICING_USD_PER_MTOK, DEFAULT_PRICE
from .store import (
    SPEND_PROVIDERS,
    _conn,
    _tzinfo,
    daily_spend_series,
    init,
    latest_snapshots,
    read_attribution,
)


def _dashboard_url() -> str:
    return os.getenv("DASHBOARD_URL", "http://localhost:8770").rstrip("/")


def build_digest_payload(tz=None, now_ts: Optional[int] = None) -> dict[str, Any]:
    """Local-day spend per provider vs. 7-full-day average + top model spend.

    `tz` / `now_ts` exist for deterministic tests; production uses the box's
    local zone and the current time.
    """
    init()
    tzi = _tzinfo(tz)
    now = datetime.fromtimestamp(now_ts, tzi) if now_ts else datetime.now(tzi)
    today_str = now.strftime("%Y-%m-%d")

    by_provider: dict[str, dict[str, Any]] = {}
    for prov in SPEND_PROVIDERS:
        series = daily_spend_series(prov, 8, tz=tzi, now_ts=now_ts)
        today_val = series[-1][1]            # today, partial
        prior = [v for _d, v in series[:-1]]  # 7 full prior local days
        avg7 = (sum(prior) / len(prior)) if prior else 0.0
        delta_pct = ((today_val - avg7) / avg7 * 100) if avg7 > 0 else None
        by_provider[prov] = {
            "today": round(today_val, 4),
            "avg7": round(avg7, 4),
            "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        }

    snap = latest_snapshots().get("deepgram") or {}
    deepgram_balance = snap.get("balance_usd")

    # Top 5 spending models from local usage_reports, since local midnight.
    local_midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with _conn() as c:
        model_rows = c.execute(
            """
            SELECT model, SUM(input_tokens), SUM(output_tokens)
            FROM usage_reports
            WHERE ts >= ?
            GROUP BY model
            """,
            (local_midnight,),
        ).fetchall()

    top_models: list[tuple[str, float]] = []
    for model, in_tok, out_tok in model_rows:
        in_p, out_p = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICE)
        usd = ((in_tok or 0) * in_p + (out_tok or 0) * out_p) / 1_000_000
        if usd > 0:
            top_models.append((model, round(usd, 5)))
    top_models.sort(key=lambda x: -x[1])
    top_models = top_models[:5]

    total_today = round(sum(d["today"] for d in by_provider.values()), 4)
    total_avg7 = round(sum(d["avg7"] for d in by_provider.values()), 4)

    # Per-source attribution (API keys / projects / ingest tags) over the
    # digest window — sources with no spend in 8 days are omitted.
    attribution = read_attribution(8, tz=tzi)
    by_app = [
        {"source": s["source"], "today": s["today"], "total_8d": s["total"]}
        for s in attribution["sources"]
        if s["total"] > 0
    ][:10]

    return {
        "date": today_str,
        "as_of": now.strftime("%H:%M %Z"),
        "total_today": total_today,
        "total_avg7": total_avg7,
        "by_provider": by_provider,
        "by_app": by_app,
        "deepgram_balance": deepgram_balance,
        "top_models": top_models,
        "dashboard_url": _dashboard_url(),
    }


def render_text(p: dict[str, Any]) -> str:
    header = f"costwatch — {p['date']}"
    if p.get("as_of"):
        header += f" (as of {p['as_of']})"
    L = [
        header,
        "",
        f"Total today:   ${p['total_today']:>9.4f}",
        f"7-day avg:     ${p['total_avg7']:>9.4f}",
        "",
        "By provider:",
    ]
    for prov, d in p["by_provider"].items():
        delta = ""
        if d["delta_pct"] is not None:
            sign = "+" if d["delta_pct"] >= 0 else ""
            delta = f"  ({sign}{d['delta_pct']:.0f}% vs avg)"
        L.append(f"  {prov:10s} ${d['today']:>9.4f}  (7d avg ${d['avg7']:>8.4f}){delta}")

    if p.get("by_app"):
        L += ["", "By app (today / last 8 days):"]
        for a in p["by_app"]:
            L.append(f"  {a['source']:35s} ${a['today']:>9.4f} / ${a['total_8d']:>9.4f}")

    if p["deepgram_balance"] is not None:
        L += ["", f"Deepgram balance: ${p['deepgram_balance']:>9.2f} remaining"]

    if p["top_models"]:
        L += ["", "Top models today (local tracking):"]
        for model, usd in p["top_models"]:
            L.append(f"  {model:35s} ${usd:.5f}")

    L += ["", f"Dashboard: {p['dashboard_url']}"]
    return "\n".join(L)


def render_html(p: dict[str, Any]) -> str:
    rows = ""
    for prov, d in p["by_provider"].items():
        delta = ""
        if d["delta_pct"] is not None:
            sign = "+" if d["delta_pct"] >= 0 else ""
            color = "#888" if abs(d["delta_pct"]) < 10 else ("#d97706" if d["delta_pct"] > 0 else "#059669")
            delta = f'<span style="color:{color}">{sign}{d["delta_pct"]:.0f}%</span>'
        rows += (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{prov}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-variant-numeric:tabular-nums;">${d["today"]:.4f}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#888;font-variant-numeric:tabular-nums;">${d["avg7"]:.4f}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">{delta}</td>'
            f'</tr>'
        )

    by_app_html = ""
    if p.get("by_app"):
        import html as _html
        ar = "".join(
            f'<tr><td style="padding:4px 12px;color:#444;">{_html.escape(a["source"])}</td>'
            f'<td style="padding:4px 12px;text-align:right;font-variant-numeric:tabular-nums;">${a["today"]:.4f}</td>'
            f'<td style="padding:4px 12px;text-align:right;color:#888;font-variant-numeric:tabular-nums;">${a["total_8d"]:.4f}</td></tr>'
            for a in p["by_app"]
        )
        by_app_html = (
            '<h3 style="color:#666;margin-top:32px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">By app (today / 8 days)</h3>'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{ar}</table>'
        )

    deepgram_html = ""
    if p["deepgram_balance"] is not None:
        deepgram_html = (
            f'<p style="color:#666;margin-top:24px;">Deepgram balance: '
            f'<strong>${p["deepgram_balance"]:.2f}</strong> remaining</p>'
        )

    top_html = ""
    if p["top_models"]:
        mr = "".join(
            f'<tr><td style="padding:4px 12px;color:#444;">{m}</td>'
            f'<td style="padding:4px 12px;text-align:right;font-variant-numeric:tabular-nums;">${u:.5f}</td></tr>'
            for m, u in p["top_models"]
        )
        top_html = (
            '<h3 style="color:#666;margin-top:32px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">Top models today</h3>'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{mr}</table>'
        )

    return (
        '<html><body style="font-family:system-ui,-apple-system,sans-serif;color:#222;max-width:600px;margin:0 auto;padding:24px;">'
        f'<p style="color:#666;margin:0;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">costwatch · {p["date"]}{(" · as of " + p["as_of"]) if p.get("as_of") else ""}</p>'
        f'<h1 style="font-size:42px;margin:6px 0 4px;font-weight:700;letter-spacing:-0.02em;">${p["total_today"]:.4f}</h1>'
        '<p style="color:#666;margin-top:0;">total spent today</p>'
        '<table style="width:100%;border-collapse:collapse;margin-top:20px;font-size:14px;">'
        '<thead><tr style="color:#666;font-size:11px;text-transform:uppercase;letter-spacing:0.05em;">'
        '<th style="text-align:left;padding:8px 12px;border-bottom:1px solid #ddd;">provider</th>'
        '<th style="text-align:right;padding:8px 12px;border-bottom:1px solid #ddd;">today</th>'
        '<th style="text-align:right;padding:8px 12px;border-bottom:1px solid #ddd;">7-day avg</th>'
        '<th style="text-align:right;padding:8px 12px;border-bottom:1px solid #ddd;">vs avg</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'{by_app_html}{deepgram_html}{top_html}'
        f'<p style="margin-top:32px;font-size:13px;"><a href="{p["dashboard_url"]}" style="color:#0d6efd;text-decoration:none;">Open dashboard →</a></p>'
        '</body></html>'
    )
