"""Token cost estimation from configured prices. Unknown price -> None (never guessed)."""

from research_autopilot.config import ModelPrice
from research_autopilot.planning.agents import Usage


def estimate_cost(usage: Usage, price: ModelPrice | None) -> float | None:
    if price is None:
        return None
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    cached_price = price.cached_input_per_1m if price.cached_input_per_1m is not None else price.input_per_1m
    return round(
        ((usage.input_tokens - cached) * price.input_per_1m
         + cached * cached_price
         + usage.output_tokens * price.output_per_1m) / 1_000_000,
        6,
    )
