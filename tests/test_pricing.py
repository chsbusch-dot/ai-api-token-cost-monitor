"""Current Claude models are priced from the table, not the Sonnet fallback."""
from __future__ import annotations

from costwatch.core.billing import DEFAULT_PRICE, PRICING_USD_PER_MTOK
from costwatch.core.billing_fetchers import _usage_row_cost_usd


def test_current_claude_models_are_in_the_table():
    for model in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
                  "claude-haiku-5-5", "claude-fable-5-1"):
        assert model in PRICING_USD_PER_MTOK, model


def test_opus_5_5_row_cost_uses_its_own_price():
    row = {"model": "claude-opus-5-5", "uncached_input_tokens": 1_000_000,
           "output_tokens": 1_000_000}
    assert _usage_row_cost_usd(row) == 4.00 + 20.00
    fallback = DEFAULT_PRICE[0] + DEFAULT_PRICE[1]
    assert _usage_row_cost_usd(row) != fallback
