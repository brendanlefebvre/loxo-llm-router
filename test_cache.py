"""Tests for B1 cache injection (mechanism `auto` per the 2026-07 spike,
validated live 2026-07-19: ~98-99% of prompt cached turn-over-turn)."""

from loxo_llm_router import cache

CARD = {"input_per_mtok": 3.0, "cache_read_per_mtok": 0.3}
NO_CACHE_CARD = {"input_per_mtok": 1.0, "cache_read_per_mtok": None}


def test_supports_cache():
    assert cache.supports_cache(CARD) is True
    assert cache.supports_cache(NO_CACHE_CARD) is False
    assert cache.supports_cache(None) is False
    assert cache.supports_cache({}) is False


def test_inject_on_supported_target():
    body = {"model": "anthropic/claude-sonnet-4.6", "messages": []}
    cache.inject_cache(body, CARD)
    assert body["cache_control"] == {"type": "ephemeral"}


def test_never_overrides_client_cache_control():
    body = {"cache_control": {"type": "ephemeral", "ttl": "1h"}}
    cache.inject_cache(body, CARD)
    assert body["cache_control"]["ttl"] == "1h"


def test_no_injection_without_card_support():
    body = {}
    cache.inject_cache(body, NO_CACHE_CARD)
    cache.inject_cache(body, None)
    assert "cache_control" not in body


def test_estimate_savings():
    usage = {"prompt_tokens_details": {"cached_tokens": 1_000_000}}
    assert cache.estimate_savings_usd(usage, CARD) == 2.7  # (3.0-0.3) per mtok
    assert cache.estimate_savings_usd(usage, NO_CACHE_CARD) == 0.0
    assert cache.estimate_savings_usd(None, CARD) == 0.0
    assert cache.estimate_savings_usd({}, CARD) == 0.0


def test_estimate_savings_clamped_to_zero_for_negative_input_rate():
    # A zero (or otherwise below cache_read_per_mtok) input rate must never
    # produce a negative "savings" figure.
    usage = {"prompt_tokens_details": {"cached_tokens": 1_000_000}}
    negative_card = {"input_per_mtok": 0.0, "cache_read_per_mtok": 0.3}
    assert cache.estimate_savings_usd(usage, negative_card) == 0.0
