"""Token pricing for the models Mise runs (cost roadmap phase 0).

Rates are USD per million tokens (Claude API list prices, 2026-09). Cache
reads bill at 0.1x the input rate, cache writes at 1.25x.
"""

PRICING_PER_MTOK = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
FALLBACK_RATES = PRICING_PER_MTOK["claude-opus-5"]  # price unknown models high


def rates_for(model: str) -> tuple[float, float]:
    for key, rates in PRICING_PER_MTOK.items():
        if model == key or model.startswith(key + "-"):
            return rates
    return FALLBACK_RATES


def usage_cost(model: str, usage) -> float:
    """Dollar cost of one API response's usage object."""
    in_rate, out_rate = rates_for(model)
    get = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
    return (
        get("input_tokens") * in_rate
        + get("cache_read_input_tokens") * in_rate * 0.1
        + get("cache_creation_input_tokens") * in_rate * 1.25
        + get("output_tokens") * out_rate
    ) / 1_000_000
