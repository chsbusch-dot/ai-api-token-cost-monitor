"""Regression tests for the Anthropic prepaid /credits response parser."""
from __future__ import annotations

from costwatch.core.billing_fetchers import _parse_balance_usd


def test_parses_real_anthropic_response_shape():
    """Verified shape from platform.claude.com on 2026-05-08:
        {"amount": 672, "currency": "USD", "auto_reload_settings": {...}, ...}
    `amount` is in cents — 672 = $6.72.
    """
    data = {
        "amount": 672,
        "currency": "USD",
        "auto_reload_settings": {
            "enabled": True,
            "threshold_in_minor_units": 500,
            "reload_to_in_minor_units": 1500,
        },
        "pending_invoice_amount_cents": None,
        "last_paid_purchase_cents": None,
    }
    assert _parse_balance_usd(data) == 6.72


def test_handles_zero_balance():
    assert _parse_balance_usd({"amount": 0, "currency": "USD"}) == 0.0


def test_handles_large_balance():
    assert _parse_balance_usd({"amount": 1234567, "currency": "USD"}) == 12345.67


def test_handles_missing_currency_defaults_usd():
    """If currency is omitted, we still treat amount as cents."""
    assert _parse_balance_usd({"amount": 1000}) == 10.0


def test_returns_none_on_unrecognized_shape():
    assert _parse_balance_usd({"weird": "shape"}) is None
    assert _parse_balance_usd("not a dict") is None
    assert _parse_balance_usd(None) is None


def test_fallback_handles_dollar_string():
    """Defensive fallback for hypothetical alternative shapes."""
    assert _parse_balance_usd({"balance": "$42.50"}) == 42.50
    assert _parse_balance_usd({"available_credits_usd": 99.99}) == 99.99


def test_fallback_handles_nested_credits():
    assert _parse_balance_usd({"credits": {"available_credits_usd": 50.0}}) == 50.0
