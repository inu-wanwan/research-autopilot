from datetime import datetime, timezone
from types import SimpleNamespace

from research_autopilot.autopilot.budget import chain_usage
from research_autopilot.models import LlmCall

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)


def _call(**kw) -> LlmCall:
    base = dict(agent="reviewer", model="gpt-5.6-sol", iteration=0, attempt=1, started_at=NOW, finished_at=NOW, ok=True)
    return LlmCall(**{**base, **kw})


def _store(calls: list[LlmCall]):
    rec = SimpleNamespace(cost=SimpleNamespace(estimated_cost_usd=None), llm_calls=calls,
                          literature_cost_usd=0.0, external_cost_usd=0.0)
    return SimpleNamespace(get_run=lambda _id: rec)


def test_failed_calls_without_a_price_are_not_counted_as_unknown_paid_calls():
    # INU-364 (2026-10-14): the planner ran on the subscription, then three reviewer calls failed with
    # credit_balance_exhausted. Nothing was charged, so the budget must stay verifiable.
    calls = [_call(agent="planner", estimated_cost_usd=0.0, billing="subscription", input_tokens=10, output_tokens=5)]
    calls += [_call(ok=False, attempt=n, error="transient: RateLimitError credit_balance_exhausted") for n in (1, 2, 3)]
    assert chain_usage(_store(calls), [{"plan_run_id": "p"}]).unknown_api_cost_calls == 0


def test_successful_calls_without_a_price_are_still_counted():
    calls = [_call(input_tokens=100, output_tokens=50, billing="api"),
             _call(ok=False, attempt=2, error="transient: timeout")]
    assert chain_usage(_store(calls), [{"plan_run_id": "p"}]).unknown_api_cost_calls == 1
