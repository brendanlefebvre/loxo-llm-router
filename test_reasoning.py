"""Tests for B2: tier reasoning knob -> OpenRouter `reasoning` param."""

import loxo_llm_router as R
from loxo_llm_router.config import VirtualModel


def _vm(reasoning=None):
    return VirtualModel(id="loxo/deep", cloud_target="anthropic/claude-sonnet-4.6",
                        routing="cloud", reasoning=reasoning)


def test_tier_knob_injects_effort():
    body = {"model": "x"}
    R.apply_reasoning(body, _vm("high"))
    assert body["reasoning"] == {"effort": "high"}


def test_no_knob_no_injection():
    body = {"model": "x"}
    R.apply_reasoning(body, _vm(None))
    R.apply_reasoning(body, None)
    assert "reasoning" not in body


def test_client_reasoning_wins():
    body = {"reasoning": {"max_tokens": 512}}
    R.apply_reasoning(body, _vm("high"))
    assert body["reasoning"] == {"max_tokens": 512}


def test_openai_reasoning_effort_translated_and_wins():
    body = {"reasoning_effort": "low"}
    R.apply_reasoning(body, _vm("high"))
    assert body["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in body


def test_config_parses_reasoning_field():
    vm = _vm("medium")
    assert vm.reasoning == "medium"


def test_config_rejects_bad_reasoning_value(tmp_path, monkeypatch):
    import pytest
    from loxo_llm_router.config import load_config
    (tmp_path / "loxo.toml").write_text(
        '[tiers.bad]\nrouting = "cloud"\ncloud_target = "x/y"\nreasoning = "extreme"\n')
    monkeypatch.setenv("LOXO_CONFIG", str(tmp_path / "loxo.toml"))
    with pytest.raises(ValueError, match="reasoning"):
        load_config()
