"""Token usage -> USD cost estimation."""

from __future__ import annotations

from issuepilot.llm.base import Usage


def estimate_cost_usd(usage: Usage, price_input_per_m: float, price_output_per_m: float) -> float:
    return (
        usage.prompt_tokens * price_input_per_m + usage.completion_tokens * price_output_per_m
    ) / 1_000_000


def format_usage(usage: Usage, cost_usd: float) -> str:
    return (
        f"tokens: {usage.prompt_tokens} prompt + {usage.completion_tokens} completion "
        f"= {usage.total_tokens} total | est. cost: ${cost_usd:.6f}"
    )
