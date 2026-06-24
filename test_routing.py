"""Pure-function tests for the router's routing logic. No network, no server.

Run from the repo root with the router's venv:
    /Users/brendanl/.venvs/mlx/bin/python3 -m pytest test_routing.py -q

These are characterization tests: they lock the CURRENT behavior of
`pick_target`, `estimate_prompt_tokens`, and `is_local_model` so the additive
virtual-model branch (see docs/superpowers/specs/2026-06-22-virtual-model-
abstraction-design.md) can be built without silently regressing existing routes.
"""

import importlib

import pytest

import llm_router as R


@pytest.fixture(autouse=True)
def routing_env(monkeypatch):
    """Pin the routing-relevant module globals to known values per test, so the
    tests don't depend on the ambient environment the module was imported with."""
    monkeypatch.setattr(R, "LOCAL_MODELS", {"qwen3", "mlx-community/Qwen3.6-35B-A3B-4bit"})
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 60000)
    monkeypatch.setattr(R, "LOCAL_BASE_URL", "http://local.test/v1")
    monkeypatch.setattr(R, "CLOUD_BASE_URL", "https://cloud.test/v1")
    monkeypatch.setattr(R, "CLOUD_DEFAULT_MODEL", "anthropic/claude-sonnet-4.6")
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", ["mlx-community/Qwen3.6-35B-A3B-4bit"])
    monkeypatch.setattr(R, "VIRTUAL_MODELS", {
        "airwolf/auto": R.VirtualModel(id="airwolf/auto", cloud_target="z-ai/glm-5.2"),
    })
    yield


def _body(model="", text="hello", tools=None):
    b = {"model": model, "messages": [{"role": "user", "content": text}]}
    if tools is not None:
        b["tools"] = tools
    return b


# --- pick_target: one test per routing rule (first match wins) ----------------

def test_rule1_explicit_local_model_routes_local():
    base, model, reason = R.pick_target(_body(model="qwen3-30b-a3b"), None)
    assert base == R.LOCAL_BASE_URL
    assert model == "qwen3-30b-a3b"  # passed through untouched
    assert reason == "explicit-local-model"


def test_rule1_beats_quality_best():
    # A local model id wins even with x-quality: best (rule 1 precedes rule 2).
    base, _, reason = R.pick_target(_body(model="qwen3-30b"), "best")
    assert base == R.LOCAL_BASE_URL
    assert reason == "explicit-local-model"


def test_rule2_quality_best_routes_cloud():
    base, model, reason = R.pick_target(_body(model="some-model"), "best")
    assert base == R.CLOUD_BASE_URL
    assert model == "some-model"
    assert reason == "quality-best"


def test_rule2_quality_best_empty_model_uses_default():
    base, model, reason = R.pick_target(_body(model=""), "BEST")  # case-insensitive
    assert base == R.CLOUD_BASE_URL
    assert model == R.CLOUD_DEFAULT_MODEL
    assert reason == "quality-best"


def test_rule3_prompt_too_long_routes_cloud(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 10)  # tiny limit
    big = "x" * 1000  # ~250 tokens > 10
    base, _, reason = R.pick_target(_body(model="local-ish", text=big), None)
    assert base == R.CLOUD_BASE_URL
    assert reason == "prompt-too-long"


def test_rule4_provider_prefixed_routes_cloud():
    base, model, reason = R.pick_target(_body(model="anthropic/claude-sonnet-4.6"), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "anthropic/claude-sonnet-4.6"
    assert reason == "provider-prefixed"


def test_rule5_default_local():
    base, model, reason = R.pick_target(_body(model="mystery-model"), None)
    assert base == R.LOCAL_BASE_URL
    assert model == "mystery-model"
    assert reason == "default-local"


# --- estimate_prompt_tokens ---------------------------------------------------

def test_estimate_counts_message_text():
    # 40 chars / 4 = 10 tokens
    assert R.estimate_prompt_tokens(_body(text="a" * 40)) == 10


def test_estimate_counts_tool_schemas():
    # Tool/function schemas are counted too (agentic clients send large ones).
    no_tools = R.estimate_prompt_tokens(_body(text="hi"))
    with_tools = R.estimate_prompt_tokens(_body(text="hi", tools=[{"x": "y" * 400}]))
    assert with_tools > no_tools


def test_estimate_counts_list_content_text_parts():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "b" * 80},
        {"type": "image_url", "image_url": {"url": "data:..."}},  # not counted as text
    ]}]}
    assert R.estimate_prompt_tokens(body) == 20  # 80/4, image part ignored


# --- is_local_model -----------------------------------------------------------

def test_is_local_model_substring_match():
    assert R.is_local_model("qwen3-30b-a3b") is True


def test_is_local_model_no_match():
    assert R.is_local_model("gpt-4o") is False


def test_is_local_model_empty_registry(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_MODELS", set())
    assert R.is_local_model("qwen3-30b") is False


def test_is_local_model_empty_name():
    assert R.is_local_model("") is False


def test_resolve_virtual_known():
    vm = R.resolve_virtual("airwolf/auto")
    assert vm is not None
    assert vm.id == "airwolf/auto"
    assert vm.cloud_target == "z-ai/glm-5.2"
    assert vm.vision == "shim"


def test_resolve_virtual_unknown_returns_none():
    assert R.resolve_virtual("z-ai/glm-5.2") is None
    assert R.resolve_virtual("") is None


def test_default_registry_has_four_tiers_with_policies():
    reg = R._build_virtual_models()
    assert set(reg) >= {"airwolf/auto", "airwolf/fast", "airwolf/deep", "airwolf/local"}
    assert reg["airwolf/auto"].routing == "auto"
    assert reg["airwolf/auto"].vision == "shim"
    assert reg["airwolf/fast"].routing == "cloud"
    assert reg["airwolf/fast"].vision == "reject"
    assert reg["airwolf/fast"].cloud_target == "z-ai/glm-4.7-flash"
    assert reg["airwolf/deep"].routing == "cloud"
    assert reg["airwolf/deep"].vision == "native"
    assert reg["airwolf/deep"].cloud_target == "google/gemini-2.5-pro"
    assert reg["airwolf/local"].routing == "local"
    assert reg["airwolf/local"].vision == "local"
    assert reg["airwolf/local"].cloud_target is None


def test_virtual_small_prompt_routes_local_with_resolved_id():
    base, model, reason = R.pick_target(_body(model="airwolf/auto", text="hi"), None)
    assert base == R.LOCAL_BASE_URL
    assert model == "mlx-community/Qwen3.6-35B-A3B-4bit"  # virtual id NOT forwarded
    assert reason == "virtual-local"


def test_virtual_quality_best_routes_cloud_target():
    base, model, reason = R.pick_target(_body(model="airwolf/auto"), "best")
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-quality-best"


def test_virtual_large_prompt_routes_cloud_target(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 10)
    base, model, reason = R.pick_target(_body(model="airwolf/auto", text="x" * 1000), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-prompt-too-long"


def test_virtual_slash_is_not_treated_as_provider_prefixed():
    # airwolf/auto contains "/" but must NOT fall to the provider-prefixed rule.
    _, _, reason = R.pick_target(_body(model="airwolf/auto", text="hi"), None)
    assert reason.startswith("virtual")


def test_local_target_for_explicit_override():
    vm = R.VirtualModel(id="x", cloud_target="c", local_target="custom-local")
    assert R.local_target_for(vm, "fallback") == "custom-local"


def test_local_target_for_empty_models_uses_fallback(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", [])
    vm = R.VirtualModel(id="x", cloud_target="c")
    assert R.local_target_for(vm, "airwolf/auto") == "airwolf/auto"


import asyncio


def _img_body():
    return {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}


def test_vision_policy_native_passes_through():
    body = _img_body()
    out = asyncio.run(R.apply_vision_policy(
        body, R.CLOUD_BASE_URL, "google/gemini-2.5-pro", "virtual-pinned-cloud",
        None, vision_policy="native"))
    assert out == (R.CLOUD_BASE_URL, "google/gemini-2.5-pro", body, "virtual-pinned-cloud")


def test_vision_policy_reject_raises_on_image():
    with pytest.raises(R.VisionRejected):
        asyncio.run(R.apply_vision_policy(
            _img_body(), R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "virtual-pinned-cloud",
            None, vision_policy="reject"))


def test_vision_policy_reject_passes_through_without_image():
    body = {"messages": [{"role": "user", "content": "just text"}]}
    out = asyncio.run(R.apply_vision_policy(
        body, R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "r", None, vision_policy="reject"))
    assert out == (R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", body, "r")


def test_vision_policy_local_no_shim_raises(monkeypatch):
    monkeypatch.setattr(R, "VISION_SHIM_MODEL", "")
    with pytest.raises(R.VisionRejected):
        asyncio.run(R.apply_vision_policy(
            _img_body(), R.LOCAL_BASE_URL, "qwen-local", "virtual-pinned-local",
            None, vision_policy="local"))


def test_vision_policy_local_thin_ocr_raises(monkeypatch):
    monkeypatch.setattr(R, "VISION_SHIM_MODEL", "some-vlm")
    monkeypatch.setattr(R, "VISION_OCR_MIN_CHARS", 50)

    async def fake_shim(body):
        return ({"shimmed": True}, 3)  # thin
    monkeypatch.setattr(R, "apply_vision_shim", fake_shim)
    with pytest.raises(R.VisionRejected):
        asyncio.run(R.apply_vision_policy(
            _img_body(), R.LOCAL_BASE_URL, "qwen-local", "virtual-pinned-local",
            None, vision_policy="local"))


def test_vision_policy_local_rich_ocr_feeds_text(monkeypatch):
    monkeypatch.setattr(R, "VISION_SHIM_MODEL", "some-vlm")
    monkeypatch.setattr(R, "VISION_OCR_MIN_CHARS", 10)

    async def fake_shim(body):
        return ({"shimmed": True}, 200)  # rich
    monkeypatch.setattr(R, "apply_vision_shim", fake_shim)
    base, model, new_body, reason = asyncio.run(R.apply_vision_policy(
        _img_body(), R.LOCAL_BASE_URL, "qwen-local", "virtual-pinned-local",
        None, vision_policy="local"))
    assert base == R.LOCAL_BASE_URL
    assert new_body == {"shimmed": True}
    assert reason.endswith("vision-local-pin")


_SAMPLE_MODELS_PAYLOAD = {"data": [
    {"id": "z-ai/glm-5.2", "context_length": 1048576,
     "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000004",
                 "input_cache_read": "0.00000018"}},
    {"id": "other/model", "pricing": {"prompt": "0.000002"}},
]}


def test_parse_rate_card_extracts_per_mtok():
    card = R._parse_rate_card(_SAMPLE_MODELS_PAYLOAD, "z-ai/glm-5.2")
    assert card is not None
    assert card["input_per_mtok"] == 1.0
    assert card["output_per_mtok"] == 4.0
    assert card["cache_read_per_mtok"] == 0.18
    assert card["context_length"] == 1048576
    assert card["input_modalities"] == ["text"]


def test_parse_rate_card_missing_fields_are_none():
    card = R._parse_rate_card(_SAMPLE_MODELS_PAYLOAD, "other/model")
    assert card is not None
    assert card["input_per_mtok"] == 2.0
    assert card["output_per_mtok"] is None
    assert card["cache_read_per_mtok"] is None


def test_parse_rate_card_unknown_id_returns_none():
    assert R._parse_rate_card(_SAMPLE_MODELS_PAYLOAD, "nope/nope") is None


def test_virtual_model_entries_shape():
    entries = R._virtual_model_entries()
    assert any(e["id"] == "airwolf/auto" for e in entries)
    e = entries[0]
    assert e["object"] == "model"
    assert e["owned_by"] == "airwolf-llm-router"
    assert "context_length" in e


def test_pinned_cloud_tier_always_cloud(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "airwolf/fast", R.VirtualModel(
        id="airwolf/fast", cloud_target="z-ai/glm-4.7-flash", routing="cloud", vision="reject"))
    base, model, reason = R.pick_target(_body(model="airwolf/fast", text="hi"), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-4.7-flash"  # virtual id NOT forwarded
    assert reason == "virtual-pinned-cloud"
    # pinned: size and quality headers do not change the lane
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1)
    base2, model2, reason2 = R.pick_target(_body(model="airwolf/fast", text="x" * 1000), "best")
    assert (base2, model2, reason2) == (R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "virtual-pinned-cloud")


def test_pinned_local_tier_always_local(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "airwolf/local", R.VirtualModel(
        id="airwolf/local", cloud_target=None, routing="local", vision="local"))
    # even with x-quality: best and a huge prompt, it stays local
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1)
    base, model, reason = R.pick_target(_body(model="airwolf/local", text="x" * 1000), "best")
    assert base == R.LOCAL_BASE_URL
    assert model == "mlx-community/Qwen3.6-35B-A3B-4bit"  # first LOCAL_MODELS entry
    assert reason == "virtual-pinned-local"


# --- cloud_fallback_for: suppression policy for pinned-local tier -----------

def test_cloud_fallback_allowed_for_auto_local():
    vm = R.VirtualModel(id="airwolf/auto", cloud_target="z-ai/glm-5.2", routing="auto")
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, vm) is True


def test_cloud_fallback_allowed_for_nonvirtual_local():
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, None) is True


def test_cloud_fallback_suppressed_for_local_pin():
    vm = R.VirtualModel(id="airwolf/local", routing="local", vision="local")
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, vm) is False


def test_cloud_fallback_none_when_base_is_cloud():
    assert R.cloud_fallback_for(R.CLOUD_BASE_URL, None) is False


def test_distinct_cloud_targets_excludes_none(monkeypatch):
    monkeypatch.setattr(R, "VIRTUAL_MODELS", {
        "a": R.VirtualModel(id="a", cloud_target="z-ai/glm-5.2", routing="auto"),
        "f": R.VirtualModel(id="f", cloud_target="z-ai/glm-4.7-flash", routing="cloud"),
        "l": R.VirtualModel(id="l", cloud_target=None, routing="local"),
    })
    assert R._distinct_cloud_targets() == {"z-ai/glm-5.2", "z-ai/glm-4.7-flash"}
