"""Logs emitted during normal operations must not contain credential values.

We provoke real code paths (mailer connection failure, deepgram fetcher with
a bogus key) and assert that *the values* of the credentials never appear in
captured log output.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

FAKE_ANTH = "sk-ant-admin-FAKETESTLOGGINGKEY-9X8C7V6B5N4M3L2K1J0H9G8F"
FAKE_OAI  = "sk-admin-FAKETESTLOGGINGKEY-A1B2C3D4E5F6G7H8I9J0K1L2"
FAKE_DG   = "FAKEdgloggingkeycafebabe1234567890abcdef0"
FAKE_PASS = "FAKEsmtpLOGPasswordYZ"


def test_mailer_does_not_log_credentials(monkeypatch, caplog):
    """Provoking a connection error should not leak SMTP_USER or SMTP_PASS."""
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "1")  # nothing listens here
    monkeypatch.setenv("SMTP_USER", "logleak@example.com")
    monkeypatch.setenv("SMTP_PASS", FAKE_PASS)
    monkeypatch.setenv("DIGEST_FROM", "from@example.com")
    monkeypatch.setenv("DIGEST_TO", "to@example.com")

    from costwatch.mailer import send

    caplog.set_level(logging.DEBUG)
    with pytest.raises(Exception):
        send("subject", "<p>x</p>", "x")

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_PASS not in log_text, "SMTP_PASS leaked into logs"


def test_deepgram_fetcher_does_not_log_key(monkeypatch, caplog):
    """A 401 from Deepgram must not surface the key value in logs."""
    from costwatch.core.billing import BillingTracker
    from costwatch.core import billing_fetchers

    # Stub httpx to return a 401 without making a real network call
    class _StubResp:
        status_code = 401
        text = "Unauthorized"

        def raise_for_status(self):
            raise httpx.HTTPStatusError("401", request=None, response=self)

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, headers=None):
            return _StubResp()

    monkeypatch.setattr(billing_fetchers.httpx, "AsyncClient", _StubClient)

    caplog.set_level(logging.DEBUG)
    tracker = BillingTracker()
    asyncio.run(billing_fetchers.fetch_deepgram_balance(FAKE_DG, tracker))

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_DG not in log_text, "Deepgram API key leaked into logs"
    # Error message should describe the failure but not echo the key
    if tracker.state.deepgram_error:
        assert FAKE_DG not in tracker.state.deepgram_error


def test_anthropic_fetcher_does_not_log_key(monkeypatch, caplog):
    from costwatch.core.billing import BillingTracker
    from costwatch.core import billing_fetchers

    class _StubResp:
        status_code = 401
        text = '{"error":"invalid"}'

        def raise_for_status(self):
            raise httpx.HTTPStatusError("401", request=None, response=self)

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, params=None, headers=None):
            return _StubResp()

    monkeypatch.setattr(billing_fetchers.httpx, "AsyncClient", _StubClient)

    caplog.set_level(logging.DEBUG)
    tracker = BillingTracker()
    result = asyncio.run(billing_fetchers.fetch_anthropic_today_cost(FAKE_ANTH, tracker))
    assert result is None  # auth disabled

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_ANTH not in log_text, "Anthropic admin key leaked into logs"
