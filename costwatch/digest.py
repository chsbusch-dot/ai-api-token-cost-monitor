"""Build today's digest from snapshots + usage_reports."""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any

from .core.billing import PRICING_USD_PER_MTOK, DEFAULT_PRICE
from .store import _conn, init


def _dashboard_url() -> str:
    return os.getenv("DASHBOARD_URL", "http://localhost:8000").rstrip("/")


def build_digest_payload() -> dict[str, Any]:
    """Today's spend per provider, vs. 7-day average + top model spend."""
    init()

    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    today_start = end - timedelta(days=1)
    week_start = end - timedelta(days=8)
    today_str = now.strftime("%Y-%m-%d")

    with _conn() as c:
        rows = c.execute(
            """
            SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS d,
                   provider, today_usd, balance_usd
            FROM snapshots
            WHERE ts >= ?
            ORDER BY ts ASC
            """,
            (int(week_start.timestamp()),),
        ).fetchall()

    fold: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for d, prov, today, balance in rows:
        fold[(d, prov)] = (today, balance)

    by_provider: dict[str, dict[str, Any]] = {}
    for prov in ("anthropic", "openai", "gemini"):
        today_val = (fold.get((today_str, prov)) or (0.0, None))[0] or 0.0
        prior = [
            (fold.get((d, prov)) or (0.0, None))[0] or 0.0
            for (d, p) in fold
            if p == prov and d != today_str
        ]
        avg7 = (sum(prior) / len(prior)) if prior else 0.0
        delta_pct = ((today_val - avg7) / avg7 * 100) if avg7 > 0 else None
        by_provider[prov] = {
            "today": round(today_val, 4),
            "avg7": round(avg7, 4),
            "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        }

    deepgram_balance = (fold.get((today_str, "deepgram")) or (None, None))[1]

    # Top 5 spending models from local usage_reports today
    today_unix = int(today_start.timestamp())
    with _conn() as c:
        model_rows = c.execute(
            """
            SELECT model, SUM(input_tokens), SUM(output_tokens)
            FROM usage_reports
            WHERE ts >= ?
            GROUP BY model
            """,
            (today_unix,),
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

    return {
        "date": today_str,
        "total_today": total_today,
        "total_avg7": total_avg7,
        "by_provider": by_provider,
        "deepgram_balance": deepgram_balance,
        "top_models": top_models,
        "dashboard_url": _dashboard_url(),
    }


def render_text(p: dict[str, Any]) -> str:
    L = [
        f"costwatch — {p['date']}",
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
        f'<p style="color:#666;margin:0;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;">costwatch · {p["date"]}</p>'
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
        f'{deepgram_html}{top_html}'
        f'<p style="margin-top:32px;font-size:13px;"><a href="{p["dashboard_url"]}" style="color:#0d6efd;text-decoration:none;">Open dashboard →</a></p>'
        '</body></html>'
    )
