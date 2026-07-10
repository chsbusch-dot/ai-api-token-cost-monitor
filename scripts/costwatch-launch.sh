#!/usr/bin/env bash
# Launch costwatch with secrets pulled from Bitwarden Secrets Manager at start.
# Non-secret config (SMTP host/port, budgets, digest addresses, org id) still
# comes from .env via the systemd EnvironmentFile; this script injects only the
# credentials, so nothing secret has to live on disk.
#
# Requires: bws (CLI), jq, and a read-only machine-account token in
# ~/.bws-token (chmod 600). Override locations with BWS_TOKEN_FILE /
# BWS_PROJECT_ID if needed.
set -euo pipefail

TOKEN_FILE="${BWS_TOKEN_FILE:-$HOME/.bws-token}"
PROJECT_ID="${BWS_PROJECT_ID:-8db24778-6c1a-4b2e-ae35-b44d00387a10}"   # Openclaw
SECRETS=(
    ANTHROPIC_ADMIN_API_KEY
    OPENAI_ADMIN_API_KEY
    DEEPGRAM_ADMIN_API_KEY
    SMTP_USER
    SMTP_PASS
    ANTHROPIC_SESSION_COOKIE
)

BWS_ACCESS_TOKEN="$(cat "$TOKEN_FILE")"
export BWS_ACCESS_TOKEN
json="$(bws secret list "$PROJECT_ID" -o json)"
unset BWS_ACCESS_TOKEN

for key in "${SECRETS[@]}"; do
    val="$(jq -r --arg k "$key" '[.[] | select(.key == $k)][0].value // empty' <<<"$json")"
    if [[ -n "$val" ]]; then
        export "$key=$val"
    else
        # Missing secrets are warnings, not fatal: costwatch degrades per
        # provider (e.g. no Deepgram key -> no Deepgram card), same as before.
        echo "costwatch-launch: secret $key not found in vault, skipping" >&2
    fi
done
unset json val

cd "$(dirname "$0")/.."
exec .venv/bin/python -m costwatch
