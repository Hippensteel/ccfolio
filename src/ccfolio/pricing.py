"""Cost calculation for Claude Code sessions."""

from __future__ import annotations

from ccfolio.models import TokenUsage

# Pricing per million tokens (USD)
# Updated: 2026-02-24
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Opus
    "claude-opus-4-6": {
        "input": 15.00,
        "output": 75.00,
        "cache_creation": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-5-20251101": {
        "input": 15.00,
        "output": 75.00,
        "cache_creation": 18.75,
        "cache_read": 1.50,
    },
    # Sonnet
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_creation": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-5-20250514": {
        "input": 3.00,
        "output": 15.00,
        "cache_creation": 3.75,
        "cache_read": 0.30,
    },
    # Haiku
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.00,
        "cache_creation": 1.00,
        "cache_read": 0.08,
    },
}

# Alias mapping for model families
MODEL_FAMILY: dict[str, str] = {}
for model_id in MODEL_PRICING:
    if "opus" in model_id:
        MODEL_FAMILY[model_id] = "Opus"
    elif "sonnet" in model_id:
        MODEL_FAMILY[model_id] = "Sonnet"
    elif "haiku" in model_id:
        MODEL_FAMILY[model_id] = "Haiku"


def get_model_family(model_id: str) -> str:
    """Get the family name (Opus, Sonnet, Haiku) for a model ID."""
    if model_id in MODEL_FAMILY:
        return MODEL_FAMILY[model_id]
    # Heuristic fallback
    lower = model_id.lower()
    if "opus" in lower:
        return "Opus"
    elif "sonnet" in lower:
        return "Sonnet"
    elif "haiku" in lower:
        return "Haiku"
    return "Unknown"


def calculate_cost(usage: TokenUsage, model: str) -> float:
    """Calculate cost in USD for a given token usage and model."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return 0.0

    cost = (
        (usage.input_tokens * pricing["input"] / 1_000_000)
        + (usage.output_tokens * pricing["output"] / 1_000_000)
        + (usage.cache_creation_tokens * pricing["cache_creation"] / 1_000_000)
        + (usage.cache_read_tokens * pricing["cache_read"] / 1_000_000)
    )
    return round(cost, 4)


def calculate_session_cost(
    turn_usages: list[tuple[TokenUsage, str]],
) -> float:
    """Calculate total cost for a session from all turns.

    Args:
        turn_usages: List of (usage, model_id) tuples from assistant turns.
    """
    total = 0.0
    for usage, model in turn_usages:
        total += calculate_cost(usage, model)
    return round(total, 4)
