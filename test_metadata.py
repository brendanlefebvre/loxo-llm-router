"""Tests for B3: /v1/models context-window truthfulness + pricing ceiling."""

import loxo_llm_router as R
from loxo_llm_router.config import VirtualModel

CARDS = {"z-ai/glm-5.2": {"context_length": 202752, "input_per_mtok": 1.0,
                          "output_per_mtok": 4.0, "cache_read_per_mtok": 0.18}}


def _entries(vms, cards):
    import loxo_llm_router as R
    old = R.VIRTUAL_MODELS
    R.VIRTUAL_MODELS = vms
    try:
        return {e["id"]: e for e in R._virtual_model_entries(cards)}
    finally:
        R.VIRTUAL_MODELS = old


def test_cloud_tier_context_from_rate_card():
    vms = {"loxo/auto": VirtualModel(id="loxo/auto", cloud_target="z-ai/glm-5.2")}
    e = _entries(vms, CARDS)["loxo/auto"]
    assert e["context_length"] == 202752


def test_explicit_advertised_context_overrides_card():
    vms = {"loxo/auto": VirtualModel(id="loxo/auto", cloud_target="z-ai/glm-5.2",
                                     advertised_context=128000)}
    assert _entries(vms, CARDS)["loxo/auto"]["context_length"] == 128000


def test_local_pinned_tier_uses_local_limit(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 60000)
    vms = {"loxo/local": VirtualModel(id="loxo/local", routing="local")}
    assert _entries(vms, CARDS)["loxo/local"]["context_length"] == 60000


def test_unknown_card_falls_back_conservatively():
    vms = {"loxo/x": VirtualModel(id="loxo/x", cloud_target="nope/nope")}
    e = _entries(vms, CARDS)["loxo/x"]
    assert e["context_length"] == 1_048_576  # documented ceiling fallback
    assert "pricing" not in e


def test_pricing_ceiling_from_card():
    vms = {"loxo/auto": VirtualModel(id="loxo/auto", cloud_target="z-ai/glm-5.2")}
    p = _entries(vms, CARDS)["loxo/auto"]["pricing"]
    assert p == {"prompt": "0.000001", "completion": "0.000004"}  # USD per token, ceiling
