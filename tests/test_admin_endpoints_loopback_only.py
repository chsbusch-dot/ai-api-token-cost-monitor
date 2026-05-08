"""The /admin/* endpoints (digest, test_email) must reject non-loopback callers.

Even if someone exposed costwatch to the LAN/Internet by mistake, the admin
surface that triggers email sends shouldn't be reachable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Importing app *after* COSTWATCH_TESTING is set in conftest avoids the poller.
from costwatch.web.server import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_admin_digest_rejects_non_loopback_client(client):
    """TestClient sets client.host to 'testclient' — not in our loopback allowlist."""
    r = client.post("/admin/digest")
    assert r.status_code == 403
    assert "loopback" in r.json().get("detail", "").lower()


def test_admin_test_email_rejects_non_loopback_client(client):
    r = client.post("/admin/test_email")
    assert r.status_code == 403


def test_index_route_is_public(client):
    """The dashboard itself is fine for LAN consumption."""
    r = client.get("/")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower() or "<html" in r.text.lower()


def test_history_endpoint_returns_only_safe_fields(client):
    r = client.get("/api/history?days=7")
    assert r.status_code == 200
    body = r.json()
    assert "series" in body
    for entry in body["series"]:
        # No surprise fields that could carry creds
        allowed = {"date", "anthropic", "openai", "gemini",
                   "deepgram_balance", "total_spend"}
        assert set(entry.keys()) <= allowed
