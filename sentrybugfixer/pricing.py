"""Model price table and cost computation.

Prices are USD per 1M tokens (input, output), per the Anthropic pricing as of 2026.
Cache reads are billed at ~0.1x input, cache writes at ~1.25x input.
"""

# model id -> (input $/MTok, output $/MTok)
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-haiku": (0.25, 1.25),
}

# Used when a model id isn't in the table (keeps cost a conservative estimate, not zero).
DEFAULT_PRICE: tuple[float, float] = (5.0, 25.0)

CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def price_for(model: str) -> tuple[float, float]:
    if model in PRICING:
        return PRICING[model]
    # tolerate date-suffixed ids like claude-haiku-4-5-20251001
    for key, price in PRICING.items():
        if model.startswith(key):
            return price
    return DEFAULT_PRICE


def is_known(model: str) -> bool:
    return model in PRICING or any(model.startswith(k) for k in PRICING)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """Estimate the USD cost of a run. input_tokens is the uncached remainder;
    cache_read / cache_write are billed at their multipliers of the input rate."""
    pin, pout = price_for(model)
    total = (
        input_tokens * pin
        + cache_write * pin * CACHE_WRITE_MULT
        + cache_read * pin * CACHE_READ_MULT
        + output_tokens * pout
    )
    return total / 1_000_000


def all_models() -> list[dict]:
    return [
        {"id": mid, "input_per_mtok": inp, "output_per_mtok": out}
        for mid, (inp, out) in PRICING.items()
    ]
