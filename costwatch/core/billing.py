from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# Per-million-token prices in USD. Conservative; defaults to Sonnet pricing for unknowns
# so we don't under-report spend. Update as vendor pricing changes.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-5": (10.00, 30.00),
    # Google Gemini
    "gemini-2.0-flash-exp": (0.0, 0.0),       # preview / free
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 5.00),           # ≤200k context; >200k tier higher (not modeled)
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-flash-8b": (0.0375, 0.15),
    "gemini-1.5-pro": (1.25, 5.00),
}

DEFAULT_PRICE = (3.00, 15.00)  # Sonnet pricing as fallback


@dataclass
class ModelSpend:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def cost_usd(self, model: str) -> float:
        in_price, out_price = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICE)
        return (self.input_tokens * in_price + self.output_tokens * out_price) / 1_000_000


@dataclass
class BillingState:
    session_started: float = field(default_factory=time.time)
    by_model: dict[str, ModelSpend] = field(default_factory=dict)
    # spend-style providers (USD spent today, UTC)
    anthropic_today_usd: Optional[float] = None
    openai_today_usd: Optional[float] = None
    gemini_today_usd: Optional[float] = None
    # balance-style providers (USD remaining; drawdown = spend, computed once we persist)
    anthropic_balance_usd: Optional[float] = None
    anthropic_balance_error: Optional[str] = None  # human-readable hint when balance is unavailable
    deepgram_balance_usd: Optional[float] = None
    deepgram_error: Optional[str] = None  # set once if balance pull fails

    def session_spend_usd(self) -> float:
        return sum(spend.cost_usd(model) for model, spend in self.by_model.items())

    def total_today_usd(self) -> Optional[float]:
        """Sum of today's spend across spend-style providers. None if no provider reported."""
        parts = [
            v for v in (self.anthropic_today_usd, self.openai_today_usd, self.gemini_today_usd)
            if v is not None
        ]
        return round(sum(parts), 4) if parts else None

    def to_payload(self) -> dict:
        return {
            "type": "billing",
            "session_spend_usd": round(self.session_spend_usd(), 4),
            "session_seconds": int(time.time() - self.session_started),
            "total_today_usd": self.total_today_usd(),
            "anthropic_today_usd": (
                round(self.anthropic_today_usd, 4) if self.anthropic_today_usd is not None else None
            ),
            "openai_today_usd": (
                round(self.openai_today_usd, 4) if self.openai_today_usd is not None else None
            ),
            "gemini_today_usd": (
                round(self.gemini_today_usd, 4) if self.gemini_today_usd is not None else None
            ),
            "anthropic_balance_usd": (
                round(self.anthropic_balance_usd, 4) if self.anthropic_balance_usd is not None else None
            ),
            "anthropic_balance_error": self.anthropic_balance_error,
            "deepgram_balance_usd": (
                round(self.deepgram_balance_usd, 4) if self.deepgram_balance_usd is not None else None
            ),
            "deepgram_error": self.deepgram_error,
            "by_model": {
                model: {
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "requests": s.requests,
                    "cost_usd": round(s.cost_usd(model), 5),
                }
                for model, s in self.by_model.items()
            },
        }


class BillingTracker:
    """Aggregates LLM token usage and provider day-totals for the running process."""

    def __init__(self):
        self.state = BillingState()
        self._lock = asyncio.Lock()

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        spend = self.state.by_model.setdefault(model, ModelSpend())
        spend.input_tokens += int(input_tokens or 0)
        spend.output_tokens += int(output_tokens or 0)
        spend.requests += 1

    def set_deepgram_balance(self, usd: float) -> None:
        self.state.deepgram_balance_usd = usd
        self.state.deepgram_error = None

    def set_deepgram_error(self, message: str) -> None:
        self.state.deepgram_error = message
        self.state.deepgram_balance_usd = None
