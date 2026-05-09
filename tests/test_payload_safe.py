"""The SSE payload must contain only a fixed allowlist of fields, none of
which can carry credential material — no env values, no headers, no key
fragments.
"""
from __future__ import annotations

import json

from costwatch.core.billing import BillingState, BillingTracker

# Every key the dashboard / SSE consumer is supposed to see. Anything else is
# an unintended leak.
ALLOWED_TOP_LEVEL_KEYS = {
    "type",
    "session_spend_usd",
    "session_seconds",
    "total_today_usd",
    "anthropic_today_usd",
    "openai_today_usd",
    "gemini_today_usd",
    "anthropic_balance_usd",
    "anthropic_balance_error",
    "deepgram_balance_usd",
    "deepgram_error",
    "by_model",
}

ALLOWED_BY_MODEL_KEYS = {"input_tokens", "output_tokens", "requests", "cost_usd"}


def test_payload_only_contains_allowlisted_keys():
    tracker = BillingTracker()
    tracker.record("claude-sonnet-4-6", 1234, 567)
    tracker.record("gpt-4o", 100, 50)
    tracker.state.anthropic_today_usd = 1.23
    tracker.state.openai_today_usd = 0.45
    tracker.state.gemini_today_usd = 0.0
    tracker.state.deepgram_balance_usd = 197.18

    payload = tracker.state.to_payload()

    assert set(payload.keys()) == ALLOWED_TOP_LEVEL_KEYS, (
        f"unexpected keys in payload: {set(payload.keys()) - ALLOWED_TOP_LEVEL_KEYS}"
    )
    for model, sub in payload["by_model"].items():
        assert set(sub.keys()) == ALLOWED_BY_MODEL_KEYS, (
            f"unexpected by_model keys for {model}: {set(sub.keys()) - ALLOWED_BY_MODEL_KEYS}"
        )


def test_payload_does_not_leak_env_values(monkeypatch):
    """Even with env populated, none of those values may surface in to_payload()."""
    fake_secrets = {
        "ANTHROPIC_ADMIN_API_KEY": "sk-ant-admin-FAKETESTKEY-9F2J3K4L5M6N7P8Q9R0S",
        "OPENAI_ADMIN_API_KEY":    "sk-admin-FAKETESTKEY-X1Y2Z3A4B5C6D7E8F9G0H1",
        "DEEPGRAM_ADMIN_API_KEY":  "FAKEdg0123456789abcdef0123456789abcdef01",
        "SMTP_PASS":               "FAKEsmtpPasswordZ9Y8X7",
    }
    for k, v in fake_secrets.items():
        monkeypatch.setenv(k, v)

    state = BillingState()
    state.anthropic_today_usd = 0.5
    state.deepgram_balance_usd = 100.0
    serialized = json.dumps(state.to_payload())

    for name, secret in fake_secrets.items():
        assert secret not in serialized, f"{name} value leaked into payload"


def test_deepgram_error_message_does_not_echo_keys(monkeypatch):
    """If deepgram_error gets set with a free-form string, it shouldn't include keys."""
    fake = "sk-ant-admin-FAKEKEY-1234567890ABCDEF1234567890ABCDEF12"
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", fake)
    state = BillingState()
    state.deepgram_error = "auth failed (check DEEPGRAM_ADMIN_API_KEY)"
    payload = state.to_payload()
    assert fake not in json.dumps(payload)
    # The error should *describe* the env var name but never its value
    assert payload["deepgram_error"] is not None
    assert "DEEPGRAM_ADMIN_API_KEY" in payload["deepgram_error"]
