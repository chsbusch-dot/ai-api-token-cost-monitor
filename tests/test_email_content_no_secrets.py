"""Email content (digest + budget alerts + test email) must not contain
provider/SMTP credential values, even when the env is populated with secrets.

Complements test_logging_redaction.py (logs) — this file covers the actual
*email body, subject, and HTTP error responses* tied to the email flow.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from costwatch.digest import render_html, render_text
from costwatch.web import server as srv


# Realistic-shaped fake values; the test asserts none appear in any email
# surface. They contain "FAKE" so the static-source scan ignores them.
FAKE_SECRETS = {
    "ANTHROPIC_ADMIN_API_KEY": "sk-ant-admin-FAKEEMAILKEY-1234567890ABCDEF1234567890ABCDEF1234567890",
    "OPENAI_ADMIN_API_KEY":    "sk-admin-FAKEEMAILKEY-09876543210987654321098765432109876543210AAA",
    "DEEPGRAM_ADMIN_API_KEY":  "FAKEemaildg40hexcafebabe1234567890abcdef0",
    "SMTP_USER":               "leakcheck@example.com",
    "SMTP_PASS":               "FAKEemailSMTPpass-do-not-log",
}

KEY_PREFIXES = ("sk-ant-admin-", "sk-ant-api-", "sk-admin-", "sk-proj-", "AIza")
ENV_VAR_NAMES_LEAKABLE = ("ANTHROPIC_ADMIN_API_KEY", "OPENAI_ADMIN_API_KEY",
                          "DEEPGRAM_ADMIN_API_KEY", "SMTP_PASS")

SAMPLE_PAYLOAD = {
    "date": "2026-05-08",
    "total_today": 4.20,
    "total_avg7": 3.50,
    "by_provider": {
        "anthropic": {"today": 2.50, "avg7": 2.00, "delta_pct": 25.0},
        "openai":    {"today": 1.20, "avg7": 1.00, "delta_pct": 20.0},
        "gemini":    {"today": 0.50, "avg7": 0.50, "delta_pct": 0.0},
    },
    "deepgram_balance": 197.18,
    "top_models": [("claude-sonnet-4-6", 2.50), ("gpt-4o", 1.20)],
    "dashboard_url": "http://example.com",
}


def _populate_env(monkeypatch):
    for k, v in FAKE_SECRETS.items():
        monkeypatch.setenv(k, v)


# ── Digest rendering ────────────────────────────────────────────────────────


def test_digest_text_does_not_leak_env_secrets(monkeypatch):
    _populate_env(monkeypatch)
    text = render_text(SAMPLE_PAYLOAD)
    for v in FAKE_SECRETS.values():
        assert v not in text, f"digest text leaked secret value"


def test_digest_html_does_not_leak_env_secrets(monkeypatch):
    _populate_env(monkeypatch)
    html = render_html(SAMPLE_PAYLOAD)
    for v in FAKE_SECRETS.values():
        assert v not in html, f"digest html leaked secret value"


def test_digest_text_contains_no_credential_prefixes():
    text = render_text(SAMPLE_PAYLOAD)
    for prefix in KEY_PREFIXES:
        assert prefix not in text


def test_digest_html_contains_no_credential_prefixes():
    html = render_html(SAMPLE_PAYLOAD)
    for prefix in KEY_PREFIXES:
        assert prefix not in html


def test_digest_does_not_inline_env_var_names():
    """The rendered email must not echo the names of secret-bearing env vars
    (which could lead a reader to look for the values nearby)."""
    text = render_text(SAMPLE_PAYLOAD)
    html = render_html(SAMPLE_PAYLOAD)
    for name in ENV_VAR_NAMES_LEAKABLE:
        assert name not in text
        assert name not in html


# ── Budget alert rendering ──────────────────────────────────────────────────


def test_budget_alert_subject_body_does_not_leak_env_secrets(monkeypatch):
    """_send_budget_alert builds subject/text/html, then sends. Patch send_mail
    to capture the message — no real SMTP call — and assert no leak."""
    _populate_env(monkeypatch)

    captured: dict[str, str] = {}

    def fake_send(subject, html, text, to=None):
        captured["subject"] = subject
        captured["html"] = html
        captured["text"] = text

    monkeypatch.setattr(srv, "send_mail", fake_send)

    fire = {
        "provider": "anthropic",
        "threshold_pct": 80,
        "spend_usd": 8.05,
        "limit_usd": 10.00,
    }
    srv._send_budget_alert(fire)

    composite = captured["subject"] + "\n" + captured["html"] + "\n" + captured["text"]
    for v in FAKE_SECRETS.values():
        assert v not in composite, "budget alert leaked a secret value"
    for prefix in KEY_PREFIXES:
        assert prefix not in composite
    for name in ENV_VAR_NAMES_LEAKABLE:
        assert name not in composite


# ── Admin endpoint error responses ──────────────────────────────────────────


def test_admin_test_email_500_response_no_secrets(monkeypatch):
    """Force an SMTP connection failure and assert the HTTP error body does
    not echo SMTP_USER / SMTP_PASS or any provider key."""
    _populate_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "1")  # nothing listens here
    monkeypatch.setenv("DIGEST_FROM", "from@example.com")
    monkeypatch.setenv("DIGEST_TO", "to@example.com")

    # Bypass loopback gating so we can hit the body of the handler
    monkeypatch.setattr(srv, "_require_loopback", lambda req: None)

    with TestClient(srv.app) as client:
        r = client.post("/admin/test_email")
        assert r.status_code >= 500, f"expected SMTP failure, got {r.status_code}"
        body = r.text
        for v in FAKE_SECRETS.values():
            assert v not in body, f"error body leaked {v[:8]}..."


def test_admin_digest_500_response_no_secrets(monkeypatch):
    """Same as above for the digest endpoint."""
    _populate_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "1")
    monkeypatch.setenv("DIGEST_FROM", "from@example.com")
    monkeypatch.setenv("DIGEST_TO", "to@example.com")

    monkeypatch.setattr(srv, "_require_loopback", lambda req: None)

    with TestClient(srv.app) as client:
        r = client.post("/admin/digest")
        assert r.status_code >= 500
        body = r.text
        for v in FAKE_SECRETS.values():
            assert v not in body


def test_admin_endpoint_with_missing_env_returns_clean_error(monkeypatch):
    """If SMTP_HOST / DIGEST_FROM / DIGEST_TO are missing, the error names
    the missing env vars but does NOT echo any populated key value."""
    # Populate provider keys but leave SMTP unset
    fake = "sk-ant-admin-FAKE-MISSINGENV-1234567890ABCDEF1234567890ABCDEF1234567890ABCD"
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", fake)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("DIGEST_FROM", raising=False)
    monkeypatch.delenv("DIGEST_TO", raising=False)

    monkeypatch.setattr(srv, "_require_loopback", lambda req: None)

    with TestClient(srv.app) as client:
        r = client.post("/admin/test_email")
        assert r.status_code == 500
        body = r.text
        # Error should mention the missing env names — but never the value
        # of any other populated key.
        assert fake not in body
        assert "SMTP_HOST" in body or "DIGEST" in body


# ── Mailer implementation guards ───────────────────────────────────────────


def test_mailer_does_not_enable_smtplib_debug():
    """smtplib's set_debuglevel(>=1) prints the AUTH command (base64 user:pass)
    to stdout. The mailer must never enable it."""
    project_root = Path(__file__).resolve().parent.parent
    src = (project_root / "costwatch" / "mailer.py").read_text()
    assert "set_debuglevel" not in src, (
        "mailer must not call smtplib's set_debuglevel — would dump AUTH credentials"
    )


def test_mailer_send_signature_does_not_accept_smuggled_headers():
    """Defensive: the public send() signature shouldn't accept arbitrary headers
    that a caller could weaponize to inject creds into outgoing mail."""
    import inspect
    from costwatch.mailer import send
    sig = inspect.signature(send)
    # We expect (subject, html, text, to=None) only
    assert set(sig.parameters.keys()) == {"subject", "html", "text", "to"}, (
        f"unexpected mailer.send signature: {sig}"
    )
