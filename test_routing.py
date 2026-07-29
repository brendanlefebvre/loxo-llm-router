"""Pure-function tests for the router's routing logic. No network, no server.

Run from the repo root with the router's venv:
    python3 -m pytest test_routing.py -q

These are characterization tests: they lock the CURRENT behavior of
`pick_target`, `estimate_prompt_tokens`, and `is_local_model` so the additive
virtual-model branch (see docs/superpowers/specs/2026-06-22-virtual-model-
abstraction-design.md) can be built without silently regressing existing routes.
"""

import asyncio
import importlib
import json

import pytest

import loxo_llm_router as R


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
    # Import-time global: conftest's env isolation lands too late to affect it,
    # since collection imports this module before any fixture runs.
    monkeypatch.setattr(R, "ROUTER_NS", "loxo")
    monkeypatch.setattr(R, "VIRTUAL_MODELS", {
        "loxo/auto": R.VirtualModel(id="loxo/auto", cloud_target="z-ai/glm-5.2"),
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
    # A local model id wins even with x-loxo-quality: best (rule 1 precedes rule 2).
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
    # Exact arithmetic, derived from the constant so recalibration
    # (Task 8) doesn't break the shape check.
    expected = int(40 / R.ESTIMATE_CHARS_PER_TOKEN)
    assert R.estimate_prompt_tokens(_body(text="a" * 40)) == expected


def test_estimate_counts_tool_schemas():
    no_tools = R.estimate_prompt_tokens(_body(text="hi"))
    with_tools = R.estimate_prompt_tokens(_body(text="hi", tools=[{"x": "y" * 400}]))
    assert with_tools > no_tools


def test_estimate_counts_list_content_text_parts():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "b" * 80},
        {"type": "image_url", "image_url": {"url": "data:..."}},  # not counted
    ]}]}
    assert R.estimate_prompt_tokens(body) == int(80 / R.ESTIMATE_CHARS_PER_TOKEN)


def test_estimate_counts_tool_calls_and_reasoning():
    """Regression for the 2026-07-28 blind spot: tool_calls,
    reasoning_content, and tool_call_id were 87% of mr-012's real prompt
    and counted as zero."""
    bare = {"messages": [{"role": "user", "content": "hi"}]}
    loaded = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "reasoning_content": "r" * 900,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read",
                                      "arguments": "{\"filePath\": \"" + "p" * 800 + "\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "c" * 700},
    ]}
    assert R.estimate_prompt_tokens(loaded) > R.estimate_prompt_tokens(bare) + (
        (900 + 800 + 700) // 5)  # loose floor: the new fields dominate


def test_count_prompt_chars_is_divisor_free():
    body = _body(text="a" * 36)
    assert R._count_prompt_chars(body) == 36


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
    vm = R.resolve_virtual("loxo/auto")
    assert vm is not None
    assert vm.id == "loxo/auto"
    assert vm.cloud_target == "z-ai/glm-5.2"
    assert vm.vision == "shim"


def test_resolve_virtual_unknown_returns_none():
    assert R.resolve_virtual("z-ai/glm-5.2") is None
    assert R.resolve_virtual("") is None


def test_default_registry_has_four_tiers_with_policies():
    reg = R.load_config().tiers
    assert set(reg) >= {"loxo/auto", "loxo/fast", "loxo/deep", "loxo/local"}
    assert reg["loxo/auto"].routing == "auto"
    assert reg["loxo/auto"].vision == "shim"
    assert reg["loxo/fast"].routing == "cloud"
    assert reg["loxo/fast"].vision == "reject"
    assert reg["loxo/fast"].cloud_target == "z-ai/glm-4.7-flash"
    assert reg["loxo/deep"].routing == "cloud"
    assert reg["loxo/deep"].vision == "native"
    assert reg["loxo/deep"].cloud_target == "google/gemini-2.5-pro"
    assert reg["loxo/local"].routing == "local"
    assert reg["loxo/local"].vision == "local"
    assert reg["loxo/local"].cloud_target is None


def test_virtual_small_prompt_routes_local_with_resolved_id():
    base, model, reason = R.pick_target(_body(model="loxo/auto", text="hi"), None)
    assert base == R.LOCAL_BASE_URL
    assert model == "mlx-community/Qwen3.6-35B-A3B-4bit"  # virtual id NOT forwarded
    assert reason == "virtual-local"


def test_virtual_quality_best_routes_cloud_target():
    base, model, reason = R.pick_target(_body(model="loxo/auto"), "best")
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-quality-best"


def test_virtual_large_prompt_routes_cloud_target(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 10)
    base, model, reason = R.pick_target(_body(model="loxo/auto", text="x" * 1000), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-prompt-too-long"


def test_virtual_slash_is_not_treated_as_provider_prefixed():
    # loxo/auto contains "/" but must NOT fall to the provider-prefixed rule.
    _, _, reason = R.pick_target(_body(model="loxo/auto", text="hi"), None)
    assert reason.startswith("virtual")


def test_local_target_for_explicit_override():
    vm = R.VirtualModel(id="x", cloud_target="c", local_target="custom-local")
    assert R.local_target_for(vm, "fallback") == "custom-local"


def test_local_target_for_empty_models_uses_fallback(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_MODELS_ORDER", [])
    vm = R.VirtualModel(id="x", cloud_target="c")
    assert R.local_target_for(vm, "loxo/auto") == "loxo/auto"


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


def test_parse_all_rate_cards_covers_whole_catalog():
    cards = R._parse_all_rate_cards(_SAMPLE_MODELS_PAYLOAD)
    assert set(cards) == {"z-ai/glm-5.2", "other/model"}
    assert cards["z-ai/glm-5.2"]["cache_read_per_mtok"] == 0.18


def test_parse_all_rate_cards_skips_malformed_entry():
    """A single malformed catalog entry (unparsable pricing) must not sink the
    rest of the catalog — CodeRabbit fix 2's spirit implemented per-entry, so
    get_rate_cards()'s best-effort contract holds even before its own guard."""
    payload = {"data": [
        *_SAMPLE_MODELS_PAYLOAD["data"],
        {"id": "broken/model", "pricing": {"prompt": "not-a-number"}},
    ]}
    cards = R._parse_all_rate_cards(payload)
    assert set(cards) == {"z-ai/glm-5.2", "other/model"}
    assert "broken/model" not in cards


class _FakeChatRequest:
    """Minimal stand-in for fastapi.Request: chat_completions only awaits
    .body() and reads .headers."""
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()
        self.headers: dict[str, str] = {}

    async def body(self):
        return self._body


def test_chat_completions_triggers_rate_card_snapshot_and_injects_cache(monkeypatch):
    """fix 1: the chat path must itself call _rate_cards_snapshot_and_maybe_refresh
    (not read the raw _rate_cards global) -- otherwise cache injection is inert
    for clients that never hit /v1/models. Exercises chat_completions directly
    (not too heavy once forward() is stubbed), so this is the closer-to-
    integration form rather than a bare "function is referenced" check."""
    snapshot_calls = []
    canned_cards = {"some-model": {"input_per_mtok": 3.0, "cache_read_per_mtok": 0.3}}

    def fake_snapshot():
        snapshot_calls.append(True)
        return canned_cards
    monkeypatch.setattr(R, "_rate_cards_snapshot_and_maybe_refresh", fake_snapshot)

    captured = {}

    async def fake_forward(base_url, path, primary_body, headers, stream, **kw):
        captured["primary_body"] = primary_body
        return R.JSONResponse(content={"ok": True})
    monkeypatch.setattr(R, "forward", fake_forward)

    body = {"model": "some-model", "messages": [{"role": "user", "content": "hi"}]}
    req = _FakeChatRequest(body)
    asyncio.run(R.chat_completions(req, x_loxo_quality="best", x_loxo_vision=None, authorization=None))

    assert snapshot_calls, "chat_completions must call _rate_cards_snapshot_and_maybe_refresh"
    sent = json.loads(captured["primary_body"])
    assert sent["cache_control"] == {"type": "ephemeral"}


def test_local_streaming_body_carries_include_usage(monkeypatch):
    """stream_options.include_usage is injected unconditionally, local included.
    Without it a local streaming response has no final usage chunk, so the A2
    adequacy ledger records null tokens for every local request. mlx-lm 0.31.3
    returns the usage chunk correctly (verified 2026-07-27 against :7979)."""
    captured = {}

    async def fake_forward(base_url, path, primary_body, headers, stream, **kw):
        captured["base_url"] = base_url
        captured["primary_body"] = primary_body
        return R.JSONResponse(content={"ok": True})
    monkeypatch.setattr(R, "forward", fake_forward)

    body = {"model": "qwen3", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    req = _FakeChatRequest(body)
    asyncio.run(R.chat_completions(req, x_loxo_quality=None, x_loxo_vision=None, authorization=None))

    assert captured["base_url"] == R.LOCAL_BASE_URL, "test must exercise the local lane"
    sent = json.loads(captured["primary_body"])
    assert sent["stream_options"]["include_usage"] is True


def test_streaming_include_usage_preserves_other_stream_options(monkeypatch):
    """Injection merges into a client-supplied stream_options rather than
    replacing it."""
    captured = {}

    async def fake_forward(base_url, path, primary_body, headers, stream, **kw):
        captured["primary_body"] = primary_body
        return R.JSONResponse(content={"ok": True})
    monkeypatch.setattr(R, "forward", fake_forward)

    body = {"model": "qwen3", "messages": [{"role": "user", "content": "hi"}],
            "stream": True, "stream_options": {"some_other_flag": "keep-me"}}
    req = _FakeChatRequest(body)
    asyncio.run(R.chat_completions(req, x_loxo_quality=None, x_loxo_vision=None, authorization=None))

    sent = json.loads(captured["primary_body"])
    assert sent["stream_options"] == {"some_other_flag": "keep-me", "include_usage": True}


def test_non_streaming_body_gets_no_stream_options(monkeypatch):
    """Injection is gated on stream only -- a non-streaming body is untouched."""
    captured = {}

    async def fake_forward(base_url, path, primary_body, headers, stream, **kw):
        captured["primary_body"] = primary_body
        return R.JSONResponse(content={"ok": True})
    monkeypatch.setattr(R, "forward", fake_forward)

    body = {"model": "qwen3", "messages": [{"role": "user", "content": "hi"}]}
    req = _FakeChatRequest(body)
    asyncio.run(R.chat_completions(req, x_loxo_quality=None, x_loxo_vision=None, authorization=None))

    assert "stream_options" not in json.loads(captured["primary_body"])


def test_tier_rate_cards_filters_out_the_full_catalog(monkeypatch):
    # fix 2: /health and /v1/spend must not dump the whole ~300-model catalog --
    # only configured tier cloud_targets + CLOUD_DEFAULT_MODEL are relevant.
    monkeypatch.setitem(R.VIRTUAL_MODELS, "loxo/fast", R.VirtualModel(
        id="loxo/fast", cloud_target="z-ai/glm-4.7-flash", routing="cloud", vision="reject"))
    cards = {
        "z-ai/glm-5.2": {"model": "z-ai/glm-5.2"},          # loxo/auto's cloud_target
        "z-ai/glm-4.7-flash": {"model": "z-ai/glm-4.7-flash"},  # loxo/fast's cloud_target
        "anthropic/claude-sonnet-4.6": {"model": "anthropic/claude-sonnet-4.6"},  # CLOUD_DEFAULT_MODEL
        "some/stranger-model": {"model": "some/stranger-model"},  # rest of the catalog
    }
    out = R._tier_rate_cards(cards)
    assert set(out) == {"z-ai/glm-5.2", "z-ai/glm-4.7-flash", "anthropic/claude-sonnet-4.6"}


def test_virtual_model_entries_shape():
    entries = R._virtual_model_entries({})
    assert any(e["id"] == "loxo/auto" for e in entries)
    e = entries[0]
    assert e["object"] == "model"
    assert e["owned_by"] == "loxo-llm-router"
    assert "context_length" in e


def test_pinned_cloud_tier_always_cloud(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "loxo/fast", R.VirtualModel(
        id="loxo/fast", cloud_target="z-ai/glm-4.7-flash", routing="cloud", vision="reject"))
    base, model, reason = R.pick_target(_body(model="loxo/fast", text="hi"), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-4.7-flash"  # virtual id NOT forwarded
    assert reason == "virtual-pinned-cloud"
    # pinned: size and quality headers do not change the lane
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1)
    base2, model2, reason2 = R.pick_target(_body(model="loxo/fast", text="x" * 1000), "best")
    assert (base2, model2, reason2) == (R.CLOUD_BASE_URL, "z-ai/glm-4.7-flash", "virtual-pinned-cloud")


def test_pinned_local_tier_always_local(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "loxo/local", R.VirtualModel(
        id="loxo/local", cloud_target=None, routing="local", vision="local"))
    # even with x-loxo-quality: best and a huge prompt, it stays local
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1)
    base, model, reason = R.pick_target(_body(model="loxo/local", text="x" * 1000), "best")
    assert base == R.LOCAL_BASE_URL
    assert model == "mlx-community/Qwen3.6-35B-A3B-4bit"  # first LOCAL_MODELS entry
    assert reason == "virtual-pinned-local"


# --- cloud_fallback_for: suppression policy for pinned-local tier -----------

def test_cloud_fallback_allowed_for_auto_local():
    vm = R.VirtualModel(id="loxo/auto", cloud_target="z-ai/glm-5.2", routing="auto")
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, vm) is True


def test_cloud_fallback_allowed_for_nonvirtual_local():
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, None) is True


def test_cloud_fallback_suppressed_for_local_pin():
    vm = R.VirtualModel(id="loxo/local", routing="local", vision="local")
    assert R.cloud_fallback_for(R.LOCAL_BASE_URL, vm) is False


def test_cloud_fallback_none_when_base_is_cloud():
    assert R.cloud_fallback_for(R.CLOUD_BASE_URL, None) is False


# --- _local_pin_preflight: F1 clean 422 for pinned-local hard-fails ----------

def test_local_preflight_oversize_returns_422(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 10)
    vm = R.VirtualModel(id="loxo/local", routing="local", vision="local")
    body = _body(model="loxo/local", text="x" * 1000)  # ~250 tokens > 10
    resp = asyncio.run(R._local_pin_preflight(body, vm))
    assert resp is not None
    assert resp.status_code == 422


def test_local_preflight_passes_for_non_local_vm():
    vm = R.VirtualModel(id="loxo/auto", cloud_target="z-ai/glm-5.2", routing="auto")
    assert asyncio.run(R._local_pin_preflight(_body(model="loxo/auto"), vm)) is None


def test_local_preflight_passes_for_no_vm():
    assert asyncio.run(R._local_pin_preflight(_body(model="x"), None)) is None


def test_local_preflight_unreachable_returns_422(monkeypatch):
    monkeypatch.setattr(R, "LOCAL_CONTEXT_LIMIT", 1_000_000)  # not oversize

    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise R.httpx.ConnectError("down")

    monkeypatch.setattr(R.httpx, "AsyncClient", _Boom)
    vm = R.VirtualModel(id="loxo/local", routing="local", vision="local")
    resp = asyncio.run(R._local_pin_preflight(_body(model="loxo/local", text="hi"), vm))
    assert resp is not None
    assert resp.status_code == 422


# --- F3: local tier advertises real context limit ----------------------------

def test_local_tier_advertises_local_context_limit(monkeypatch):
    reg = R.load_config().tiers
    assert reg["loxo/local"].advertised_context is None
    monkeypatch.setattr(R, "VIRTUAL_MODELS", reg)
    assert ({e["id"]: e for e in R._virtual_model_entries({})}["loxo/local"]["context_length"]
            == R.LOCAL_CONTEXT_LIMIT)


# --- forward(): streaming surfaces upstream non-200 (no masked HTTP 200) ------

class _FakeResp:
    def __init__(self, status_code, chunks=None, body=b""):
        self.status_code = status_code
        self._chunks = list(chunks or [])
        self._body = body
        self.closed = False

    async def aread(self):
        return self._body

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


def _client_factory(resp, sink=None):
    """Fake httpx.AsyncClient bound to one canned response, supporting the
    build_request()/send() streaming API used by forward(). If `sink` is given,
    each constructed client is appended to it so a test can assert it was closed."""
    class _C:
        def __init__(self, *a, **k):
            self.closed = False
            if sink is not None:
                sink.append(self)

        def build_request(self, *a, **k):
            return ("request",)

        async def send(self, request, stream=False):
            return resp

        async def aclose(self):
            self.closed = True
    return _C


def test_forward_stream_non200_surfaces_status(monkeypatch):
    # A streamed cloud request whose upstream 402s must surface as 402, not a
    # masked HTTP 200 carrying the error body as if it were SSE.
    resp = _FakeResp(402, body=b'{"error":{"message":"Insufficient credits","code":402}}')
    clients = []
    monkeypatch.setattr(R.httpx, "AsyncClient", _client_factory(resp, clients))
    out = asyncio.run(R.forward(
        R.CLOUD_BASE_URL, "/chat/completions", b"{}", {}, True,
        cloud_model="z-ai/glm-4.7-flash", cloud_provider="openrouter.ai",
        reason="virtual-pinned-cloud"))
    assert isinstance(out, R.JSONResponse)
    assert out.status_code == 402
    assert b"Insufficient credits" in out.body
    assert resp.closed is True                     # response drained + closed
    assert clients and clients[0].closed is True   # client not leaked


def test_forward_stream_200_passes_chunks(monkeypatch):
    chunks = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b'data: [DONE]\n\n']
    resp = _FakeResp(200, chunks=chunks)
    clients = []
    monkeypatch.setattr(R.httpx, "AsyncClient", _client_factory(resp, clients))

    async def _run():
        out = await R.forward(
            R.CLOUD_BASE_URL, "/chat/completions", b"{}", {}, True,
            cloud_model=None, cloud_provider=None, reason="x")
        assert isinstance(out, R.StreamingResponse)
        return out, [c async for c in out.body_iterator]

    out, got = asyncio.run(_run())
    assert b"".join(got) == b"".join(chunks)
    assert resp.closed is True                     # finally closed the response
    assert clients and clients[0].closed is True   # and the client


# --- forward(): fall back to cloud when local crashes/disconnects (F-A) -------

def _client_factory_fail_then_ok(exc, ok_resp, sink=None):
    """Fake httpx.AsyncClient whose first send() raises `exc` and whose second
    send() returns `ok_resp` — models a local server that disconnects, then a
    healthy cloud fallback."""
    class _C:
        def __init__(self, *a, **k):
            self.closed = False
            self.calls = 0
            if sink is not None:
                sink.append(self)

        def build_request(self, *a, **k):
            return ("request",)

        async def send(self, request, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise exc
            return ok_resp

        async def aclose(self):
            self.closed = True
    return _C


def test_remote_protocol_error_is_a_fallback_trigger():
    # A local server that OOMs mid-prompt "disconnects without sending a
    # response" (RemoteProtocolError); that must count as wedged -> fall back.
    assert R.httpx.RemoteProtocolError in R.FALLBACK_ERRORS


def test_forward_stream_falls_back_on_remote_protocol_error(monkeypatch):
    ok = _FakeResp(200, chunks=[b"data: hi\n\n"])
    exc = R.httpx.RemoteProtocolError("Server disconnected without sending a response")
    clients = []
    monkeypatch.setattr(R.httpx, "AsyncClient", _client_factory_fail_then_ok(exc, ok, clients))

    async def _run():
        out = await R.forward(
            R.LOCAL_BASE_URL, "/chat/completions", b"{}", {}, True,
            fallback_url=R.CLOUD_BASE_URL, fallback_body=b"{}",
            cloud_model=None, cloud_provider=None, reason="virtual-local",
            fallback_cloud_model="z-ai/glm-5.2")
        return out, [c async for c in out.body_iterator]

    out, got = asyncio.run(_run())
    assert isinstance(out, R.StreamingResponse)
    assert b"".join(got) == b"data: hi\n\n"        # fallback response streamed
    assert clients[0].calls == 2                    # primary raised, fallback used


# --- log timestamps (UTC ISO-8601) -------------------------------------------

def test_ts_is_utc_iso8601_z():
    from datetime import datetime
    ts = R._ts()
    assert ts.endswith("Z")
    datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")  # parses or raises


def test_log_prepends_utc_timestamp(capsys, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(R, "QUIET", False)
    R.log("[router] hello")
    out = capsys.readouterr().out.strip()
    assert out.endswith("[router] hello")
    ts = out.split(" ", 1)[0]
    assert ts.endswith("Z")
    datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def test_uvicorn_logconfig_valid_and_timestamped():
    import json, pathlib
    # Anchored to this file, not the cwd: the suite runs from an empty directory
    # so an ambient ./loxo.toml can't reach load_config().
    cfg_path = pathlib.Path(__file__).parent / "llm-router-logconfig.json"
    cfg = json.loads(cfg_path.read_text())
    for name in ("default", "access"):
        fmt = cfg["formatters"][name]
        assert "%(asctime)s" in fmt["fmt"]
        assert fmt["datefmt"] == "%Y-%m-%dT%H:%M:%SZ"


# --- new dispatch tiers: balanced + reason (2026-06-25 spec) ------------------

def test_balanced_tier_resolves_with_policies():
    reg = R.load_config().tiers
    assert "loxo/balanced" in reg
    vm = reg["loxo/balanced"]
    assert vm.cloud_target == "z-ai/glm-5.2"
    assert vm.routing == "cloud"
    assert vm.vision == "shim"
    assert vm.advertised_context == 1_048_576


def test_reason_tier_resolves_with_policies():
    reg = R.load_config().tiers
    assert "loxo/reason" in reg
    vm = reg["loxo/reason"]
    assert vm.cloud_target == "moonshotai/kimi-k2.6"
    assert vm.routing == "cloud"
    assert vm.vision == "native"
    assert vm.advertised_context == 262_144


def test_balanced_tier_routes_cloud_target(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "loxo/balanced", R.VirtualModel(
        id="loxo/balanced", cloud_target="z-ai/glm-5.2", routing="cloud", vision="shim"))
    base, model, reason = R.pick_target(_body(model="loxo/balanced", text="hi"), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "z-ai/glm-5.2"
    assert reason == "virtual-pinned-cloud"


def test_reason_tier_routes_cloud_target(monkeypatch):
    monkeypatch.setitem(R.VIRTUAL_MODELS, "loxo/reason", R.VirtualModel(
        id="loxo/reason", cloud_target="moonshotai/kimi-k2.6",
        routing="cloud", vision="native"))
    base, model, reason = R.pick_target(_body(model="loxo/reason", text="hi"), None)
    assert base == R.CLOUD_BASE_URL
    assert model == "moonshotai/kimi-k2.6"
    assert reason == "virtual-pinned-cloud"


