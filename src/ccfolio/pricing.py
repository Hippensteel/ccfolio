"""Cost calculation for Claude Code sessions."""

from __future__ import annotations

from ccfolio.models import TokenUsage

# Pricing per million tokens (USD)
# Source: https://platform.claude.com/docs/en/docs/about-claude/pricing
# Updated: 2026-04-28 — verified live. NOTE: previous file had Opus at $15/$75
# (the old 4.1-and-prior rate). Anthropic dropped Opus 4.5+ to $5/$25 and
# Haiku 4.5 to $1/$5. Cost estimates prior to this update were ~3x too high
# for any Opus 4.5/4.6/4.7 work.
#
# cache_creation = 5-minute write multiplier (1.25x base input)
# cache_read = 0.1x base input
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Opus 4.5+ — same price tier
    "claude-opus-4-7": {
        "input": 5.00,
        "output": 25.00,
        "cache_creation": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-6": {
        "input": 5.00,
        "output": 25.00,
        "cache_creation": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-5-20251101": {
        "input": 5.00,
        "output": 25.00,
        "cache_creation": 6.25,
        "cache_read": 0.50,
    },
    # Opus 4.1 and earlier — old higher tier (kept for any historical sessions)
    "claude-opus-4-1": {
        "input": 15.00,
        "output": 75.00,
        "cache_creation": 18.75,
        "cache_read": 1.50,
    },
    # Sonnet 4-series
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
        "input": 1.00,
        "output": 5.00,
        "cache_creation": 1.25,
        "cache_read": 0.10,
    },
}

# OpenAI pricing per million tokens (USD)
# Source: https://developers.openai.com/api/docs/pricing
# Updated: 2026-04-28 — added gpt-5.5, gpt-5.5-pro, gpt-5.3-codex; corrected
# gpt-5.4 output rate ($10 → $15) and cache_read ($1.25 → $0.25). Older 5.2
# entries kept at original rates; current pricing page no longer lists them.
# Note: Codex CLI reasoning_output_tokens are added to output_tokens before costing
OPENAI_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5": {
        "input": 5.00,
        "output": 30.00,
        "cache_creation": 0.0,
        "cache_read": 0.50,
    },
    "gpt-5.5-pro": {
        "input": 30.00,
        "output": 180.00,
        "cache_creation": 0.0,
        "cache_read": 0.0,
    },
    "gpt-5.4": {
        "input": 2.50,
        "output": 15.00,
        "cache_creation": 0.0,
        "cache_read": 0.25,
    },
    "gpt-5.3-codex": {
        "input": 1.75,
        "output": 14.00,
        "cache_creation": 0.0,
        "cache_read": 0.175,
    },
    "gpt-5.2": {
        "input": 2.50,
        "output": 10.00,
        "cache_creation": 0.0,
        "cache_read": 1.25,
    },
    "gpt-5.2-codex": {
        "input": 2.50,
        "output": 10.00,
        "cache_creation": 0.0,
        "cache_read": 1.25,
    },
    "gpt-4.1": {
        "input": 2.00,
        "output": 8.00,
        "cache_creation": 0.0,
        "cache_read": 0.50,
    },
    "gpt-4.1-mini": {
        "input": 0.40,
        "output": 1.60,
        "cache_creation": 0.0,
        "cache_read": 0.10,
    },
    "gpt-4.1-nano": {
        "input": 0.10,
        "output": 0.40,
        "cache_creation": 0.0,
        "cache_read": 0.025,
    },
    "o3": {
        "input": 2.00,
        "output": 8.00,
        "cache_creation": 0.0,
        "cache_read": 0.50,
    },
    "o4-mini": {
        "input": 1.10,
        "output": 4.40,
        "cache_creation": 0.0,
        "cache_read": 0.275,
    },
}

# Gemini pricing per million tokens (USD)
# Source: https://ai.google.dev/gemini-api/docs/pricing
# Updated: 2026-04-26
# Note: gemini-3.1-pro-preview has tiered pricing — $2/$12 per Mtok for prompts
# <=200k tokens, $4/$18 for prompts >200k. Most individual chat turns stay under
# 200k, so we use the low tier. Long-context single prompts will underestimate.
# `customtools` variant uses the same base model and pricing.
GEMINI_PRICING: dict[str, dict[str, float]] = {
    "gemini-3-pro-preview": {
        "input": 2.00,
        "output": 12.00,
        "cache_creation": 0.0,
        "cache_read": 0.20,
    },
    "gemini-3.1-pro-preview": {
        "input": 2.00,
        "output": 12.00,
        "cache_creation": 0.0,
        "cache_read": 0.20,
    },
    "gemini-3.1-pro-preview-customtools": {
        "input": 2.00,
        "output": 12.00,
        "cache_creation": 0.0,
        "cache_read": 0.20,
    },
    "gemini-3-flash-preview": {
        "input": 0.50,
        "output": 3.00,
        "cache_creation": 0.0,
        "cache_read": 0.05,
    },
    "gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
        "cache_creation": 0.0,
        "cache_read": 0.03,
    },
}

# Merge all pricing
MODEL_PRICING.update(OPENAI_PRICING)
MODEL_PRICING.update(GEMINI_PRICING)

# Alias mapping for model families
MODEL_FAMILY: dict[str, str] = {}
for model_id in MODEL_PRICING:
    lower = model_id.lower()
    if "opus" in lower:
        MODEL_FAMILY[model_id] = "Opus"
    elif "sonnet" in lower:
        MODEL_FAMILY[model_id] = "Sonnet"
    elif "haiku" in lower:
        MODEL_FAMILY[model_id] = "Haiku"
    elif lower.startswith("gpt-5"):
        MODEL_FAMILY[model_id] = "GPT-5"
    elif lower.startswith("gpt-4"):
        MODEL_FAMILY[model_id] = "GPT-4"
    elif lower.startswith("o3"):
        MODEL_FAMILY[model_id] = "o3"
    elif lower.startswith("o4"):
        MODEL_FAMILY[model_id] = "o4"
    elif "gemini" in lower and "pro" in lower:
        MODEL_FAMILY[model_id] = "Gemini Pro"
    elif "gemini" in lower and "flash" in lower:
        MODEL_FAMILY[model_id] = "Gemini Flash"
    elif "gemini" in lower:
        MODEL_FAMILY[model_id] = "Gemini"


def get_model_family(model_id: str) -> str:
    """Get the family name for a model ID."""
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
    elif lower.startswith("gpt-5"):
        return "GPT-5"
    elif lower.startswith("gpt-4"):
        return "GPT-4"
    elif lower.startswith("o3"):
        return "o3"
    elif lower.startswith("o4"):
        return "o4"
    elif "gemini" in lower and "pro" in lower:
        return "Gemini Pro"
    elif "gemini" in lower and "flash" in lower:
        return "Gemini Flash"
    elif "gemini" in lower:
        return "Gemini"
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
