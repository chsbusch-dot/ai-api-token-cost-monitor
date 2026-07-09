"""Tests for the Claude Code Analytics cost parser and the
record-after-send budget alert flow."""
from __future__ import annotations

from costwatch import budgets
from costwatch.core.billing_fetchers import _sum_claude_code_costs


def test_claude_code_cost_sums_cents_across_actors_and_models():
    """Docs example: 186 + 42 cents = $2.28."""
    page = {
        "data": [
            {
                "actor": {"email_address": "u@example.com", "type": "user_actor"},
                "customer_type": "api",
                "model_breakdown": [
                    {"estimated_cost": {"amount": 186, "currency": "USD"},
                     "model": "claude-sonnet-4-6",
                     "tokens": {"input": 45230, "output": 12450, "cache_creation": 2340, "cache_read": 8790}},
                    {"estimated_cost": {"amount": 42, "currency": "USD"},
                     "model": "claude-haiku-4-5",
                     "tokens": {"input": 23100, "output": 5680, "cache_creation": 890, "cache_read": 3420}},
                ],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    assert abs(_sum_claude_code_costs([page]) - 2.28) < 1e-9


def test_claude_code_cost_empty_report_is_zero():
    assert _sum_claude_code_costs([{"data": [], "has_more": False, "next_page": None}]) == 0.0


def test_claude_code_cost_sums_across_pages():
    p1 = {"data": [{"model_breakdown": [{"estimated_cost": {"amount": 100, "currency": "USD"}}]}]}
    p2 = {"data": [{"model_breakdown": [{"estimated_cost": {"amount": 50, "currency": "USD"}}]}]}
    assert abs(_sum_claude_code_costs([p1, p2]) - 1.50) < 1e-9


def test_claude_code_cost_tolerates_missing_fields():
    weird = {"data": [{"model_breakdown": None}, {"model_breakdown": [{}]},
                      {"model_breakdown": [{"estimated_cost": {"currency": "USD"}}]}]}
    assert _sum_claude_code_costs([weird]) == 0.0


# ── budget alerts: record-after-send ────────────────────────────────────────


def test_alert_refires_until_recorded(monkeypatch):
    """evaluate() must keep returning an unrecorded alert (send failed);
    once record_sent() is called it must never fire again that day."""
    monkeypatch.setenv("BUDGET_ANTHROPIC", "10:80,100")
    spend = {"anthropic": 8.50}  # 85% of $10 → trips the 80% threshold

    first = budgets.evaluate(spend, date_str="2026-07-08")
    assert len(first) == 1
    assert first[0]["provider"] == "anthropic"
    assert first[0]["threshold_pct"] == 80

    # Simulate SMTP failure: nothing recorded → fires again
    second = budgets.evaluate(spend, date_str="2026-07-08")
    assert len(second) == 1, "unrecorded alert must retry"

    # Simulate successful send
    budgets.record_sent(second[0])
    third = budgets.evaluate(spend, date_str="2026-07-08")
    assert third == [], "recorded alert must not fire again"


def test_alert_dedup_is_per_local_date(monkeypatch):
    monkeypatch.setenv("BUDGET_ANTHROPIC", "10:100")
    spend = {"anthropic": 12.0}
    fire = budgets.evaluate(spend, date_str="2026-07-08")[0]
    budgets.record_sent(fire)
    assert budgets.evaluate(spend, date_str="2026-07-08") == []
    # New local day → fires again
    assert len(budgets.evaluate(spend, date_str="2026-07-09")) == 1
