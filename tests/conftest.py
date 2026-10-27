"""Pytest fixtures.

The most important thing this does is set COSTWATCH_TESTING=1 *before* the app
is imported anywhere, so the lifespan handler skips starting the poller (which
would otherwise make real HTTP calls during tests).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ["COSTWATCH_TESTING"] = "1"


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    """Point the SQLite store at a temp file so tests don't touch real data."""
    from costwatch import store
    # Reset the module-level connection cache between tests.
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(store, "_initialized", False)
    yield


@pytest.fixture(autouse=True)
def _no_real_admin_api(monkeypatch):
    """The digest's by-workspace section calls the Anthropic Admin API: point it at a dead
    loopback port so no test can reach the real API, whatever key the env holds."""
    from costwatch import workspaces
    monkeypatch.setattr(workspaces, "API", "http://127.0.0.1:9/v1/organizations")
    yield


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent
