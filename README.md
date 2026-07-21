# costwatch

Self-hosted, single-user real-time cost monitor for AI APIs.

Polls Anthropic, OpenAI, and Deepgram for today's spend / balance, accepts
local token reports for providers without an org-level cost API (Gemini,
custom models, anything you can POST to it), persists snapshots to SQLite,
serves a live dashboard, and emails a daily digest plus optional budget
alerts via your own SMTP.

Built because handing API keys to an unknown SaaS to monitor your own
spending is a poor trade. Keys never leave your host.

```
┌──────────────────────────────────────────────────────────────────────┐
│  costwatch (single asyncio process under systemd)                    │
│                                                                      │
│   ┌───────────────┐   ┌──────────────┐   ┌────────────────────┐      │
│   │ Anthropic API │──┐│ OpenAI API   │──┐│ Deepgram balance   │──┐   │
│   └───────────────┘  ││ /org/costs   │  ││                    │  │   │
│                      ↓└──────────────┘  ↓└────────────────────┘  ↓   │
│                  ┌──────────────────────────────────────────┐         │
│                  │ BillingTracker  (in-memory state)        │         │
│                  └─────────┬────────────┬───────────────────┘         │
│                            │            │                             │
│                            ↓            ↓                             │
│           ┌─────────────────┐    ┌──────────────────────┐             │
│           │ SQLite store    │    │ EventBus (SSE)       │             │
│           │ (snapshots,     │    │   ↓                  │             │
│           │  usage_reports, │    │ FastAPI dashboard    │             │
│           │  alerts_sent)   │    │   /api/now (live)    │             │
│           └─────┬───────────┘    │   /api/history       │             │
│                 │                │   /api/usage (POST)  │             │
│                 ↓                │   /admin/digest      │             │
│           ┌──────────────┐       └──────────────────────┘             │
│           │ daily digest │                                            │
│           │   18:00 →    │── SMTP ──→ inbox                           │
│           │   budget     │                                            │
│           │   alerts     │                                            │
│           └──────────────┘                                            │
└──────────────────────────────────────────────────────────────────────┘
```

## Features

- **Live dashboard** with Server-Sent Events — no refresh, updates every
  poll tick.
- **14-day history chart** (stacked bars per provider + Deepgram balance line).
- **Daily email digest** at a time of your choosing via systemd timer.
- **Budget alerts** with per-provider daily caps and configurable
  thresholds (80% / 100% by default), de-duplicated so each threshold
  emails at most once per day.
- **Generic ingest endpoint** for providers without an org cost API:
  `POST /api/usage` with token counts, or use the `costwatch.ingest` CLI.
- **systemd auto-restart and boot survival** (with `loginctl
  enable-linger`).
- **No external dependencies at runtime** beyond `httpx`, `fastapi`,
  `uvicorn`, and `python-dotenv`.

## Quick start (Ubuntu)

```bash
git clone https://github.com/<your-handle>/token-watch.git
cd token-watch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# Edit .env to add at least one provider key.
nano .env

# Run the daemon foreground to verify
python -m costwatch
# Open http://localhost:8770 in a browser
```

To install as a systemd user service so it runs in the background:

```bash
bash scripts/install-systemd.sh
# Optional — survive reboot without an interactive login:
sudo loginctl enable-linger "$USER"
```

The digest timer fires daily at 18:00 local time. Edit
`systemd/costwatch-digest.timer` and re-run the install script if you want
a different time.

## Configuration

All configuration is via `.env`. See [.env.example](.env.example) for the
full list. The minimum to get value from costwatch:

| Variable | What it unlocks |
|---|---|
| `ANTHROPIC_ADMIN_API_KEY` | Live Anthropic spend (admin key from console.anthropic.com → Settings → Admin Keys; org accounts only) |
| `OPENAI_ADMIN_API_KEY` | Live OpenAI spend (admin key from platform.openai.com → Organization → Admin keys) |
| `DEEPGRAM_ADMIN_API_KEY` | Deepgram remaining balance (Member or Owner role at console.deepgram.com → API Keys) |
| `SMTP_HOST`, `DIGEST_FROM`, `DIGEST_TO` | Daily email digest |

Budget caps are optional:

```
BUDGET_ANTHROPIC=10:80,100   # $10/day, alert at 80% and 100%
BUDGET_OPENAI=10:80,100
BUDGET_GEMINI=10:80,100
```

## Reporting Gemini usage

Gemini has no org-level cost API. costwatch tracks it via local token
reports. From any code that calls Gemini, capture the response's
`usage_metadata` and POST it:

```python
import requests
r = client.models.generate_content(model="gemini-2.5-pro", contents=...)
requests.post("http://localhost:8770/api/usage", json={
    "provider": "gemini",
    "model": r.model,
    "input_tokens": r.usage_metadata.prompt_token_count,
    "output_tokens": r.usage_metadata.candidates_token_count,
    "source": "my-app",
}, timeout=2)
```

Or from a shell script:

```bash
python -m costwatch.ingest gemini gemini-2.5-pro 1234 567 my-app
```

The same path works for any provider you want tracked — just pick a
provider tag and ensure the model is in the pricing table.

## Tests

The test suite specifically asserts that **no provider credential leaves
the host**:

- [test_no_real_secrets_in_source.py](tests/test_no_real_secrets_in_source.py)
  scans every committed source file for real-looking keys and fails the
  build if any match.
- [test_payload_safe.py](tests/test_payload_safe.py) verifies the SSE
  payload contains only an allowlist of fields and that env values never
  surface in serialized output.
- [test_admin_endpoints_loopback_only.py](tests/test_admin_endpoints_loopback_only.py)
  enforces that `/admin/digest` and `/admin/test_email` reject non-loopback
  callers (HTTP 403).
- [test_dashboard_no_secrets.py](tests/test_dashboard_no_secrets.py) checks
  the static HTML for any leaked key prefix or env var name.
- [test_logging_redaction.py](tests/test_logging_redaction.py) provokes
  401 responses from each provider and asserts the rejected key value
  never appears in captured log output.
- [test_email_content_no_secrets.py](tests/test_email_content_no_secrets.py)
  asserts that the rendered digest, budget alerts, and HTTP error
  responses from `/admin/digest` and `/admin/test_email` never contain
  provider keys, SMTP credentials, or env var names — even when SMTP
  fails. Also statically checks that the mailer never enables
  `smtplib.set_debuglevel` (which would dump the AUTH command).
- [test_gitignore.py](tests/test_gitignore.py) confirms `.env`, the SQLite
  files, the `data/` directory, and `.venv/` are all gitignored.

Run:

```bash
pip install -r requirements-dev.txt
pytest -v
```

CI runs the same suite plus a [gitleaks](https://github.com/gitleaks/gitleaks)
scan on every push (`.github/workflows/ci.yml`).

## Architecture

| File | Responsibility |
|---|---|
| `costwatch/core/billing.py` | `BillingState`, `BillingTracker`, the per-million-token pricing table |
| `costwatch/core/billing_fetchers.py` | Async fetchers for Anthropic Usage Report, OpenAI Cost API, Deepgram balance, plus the polling loop |
| `costwatch/store.py` | SQLite store (`snapshots`, `usage_reports`, `alerts_sent`) |
| `costwatch/events.py` | Async fan-out bus for SSE clients |
| `costwatch/web/server.py` | FastAPI app + lifespan + routes |
| `costwatch/web/index.html` | Single-page dashboard (vanilla JS + Chart.js) |
| `costwatch/digest.py` | Daily digest payload + text/HTML rendering |
| `costwatch/budgets.py` | Threshold evaluator + dedup ledger |
| `costwatch/mailer.py` | SMTP wrapper (port 25 / 465 / 587) |
| `costwatch/ingest.py` | CLI for local-token reporting |
| `systemd/*` | User-level service + 18:00 digest timer |

## Reporting semantics

All user-facing "today" figures — dashboard cards, chart bars, the digest
email, and budget alerts — are the box's **local calendar day**. Vendor
counters reset at UTC midnight, so local-day spend is reconstructed by
summing deltas between consecutive snapshots (`store.daily_spend_series`).
This matters: a digest fired at 18:00 PDT is 01:00 UTC the *next* day, and
naively reading the vendor's "today" counter at that moment reports a
1-hour-old UTC day (~$0) — the bug that shipped in v1.

## Limitations

- **Subscription/OAuth Claude Code usage is invisible to the Admin API.**
  Claude Code signed in with a Claude account (Pro/Max) bills the
  subscription — it never appears in the org's usage/cost reports and is
  not API spend. Claude Code usage billed to the org IS captured via the
  Claude Code Analytics API (`/v1/organizations/usage_report/claude_code`,
  polled every 15 min, ~1h data delay) and shows as the "claude code"
  provider.
- **Anthropic usage/cost Admin API is rate-limited to ~1 request/minute
  sustained** — run exactly one poller. costwatch backs off 5 min on 429.
- **Gemini today's spend is approximate** — local token tracking with the
  pricing table; Google added AI Studio cost dashboards (Mar 2026) but
  still no programmatic cost API for AI Studio keys.
- **Anthropic today's spend is approximate** — Usage Report API × local
  pricing table.
- **Pricing table drift** — when a provider changes prices, update
  `PRICING_USD_PER_MTOK` in `costwatch/core/billing.py` (last verified
  2026-07-08).
- **Single-user, no auth** — bind the dashboard to LAN-only or put it
  behind Tailscale / reverse-proxy auth. The `/admin/*` routes are
  loopback-only by code; non-admin routes are not.

## License

MIT — see [LICENSE](LICENSE).
