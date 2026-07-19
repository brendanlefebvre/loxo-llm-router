"""B1: prompt-cache injection for cloud-bound requests.

Mechanism: `auto` — a top-level `cache_control` field (OpenRouter enables
auto-advancing breakpoints on supporting targets). Decision and evidence:
docs/spikes/2026-07-cache-affinity.md; validated in a live OpenCode session
2026-07-19 (~98-99% of prompt cached turn-over-turn on Anthropic targets).

Pure functions; eligibility comes from the live rate card (non-null
cache_read_per_mtok). Missing card => no injection: conservative, and
self-heals once the rate-card fetch lands.
"""

from __future__ import annotations

from typing import Any


def supports_cache(card: dict[str, Any] | None) -> bool:
    return bool(card) and card.get("cache_read_per_mtok") is not None


def inject_cache(body: dict[str, Any], card: dict[str, Any] | None) -> None:
    """Mutate body in place. Client-supplied cache_control is never overridden."""
    if supports_cache(card):
        body.setdefault("cache_control", {"type": "ephemeral"})


def estimate_savings_usd(usage: dict[str, Any] | None, card: dict[str, Any] | None) -> float:
    """Counterfactual: what the cached prefix would have cost at full input price.
    0.0 whenever any input is missing — an estimate must never crash a request."""
    if not usage or not supports_cache(card):
        return 0.0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    input_rate = card.get("input_per_mtok")
    if not cached or input_rate is None:
        return 0.0
    return cached * (input_rate - card["cache_read_per_mtok"]) / 1_000_000
