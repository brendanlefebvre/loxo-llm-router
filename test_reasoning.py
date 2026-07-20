"""Tests for B2: tier reasoning knob -> OpenRouter `reasoning` param."""

import asyncio
import json

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


class _FakeChatRequest:
    """Minimal stand-in for fastapi.Request: chat_completions only awaits
    .body() and reads .headers."""
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()
        self.headers: dict[str, str] = {}

    async def body(self):
        return self._body


def test_fallback_body_gets_tier_reasoning(monkeypatch):
    """Bug: on a local-primary route with a cloud fallback, `fb = dict(body)`
    copies the LOCAL-bound primary body -- which never runs through
    apply_reasoning, since that block only fires for base_url == CLOUD_BASE_URL.
    So a client relying on the fallback (e.g. local server down) silently lost
    the tier's reasoning knob. Fix: call apply_reasoning(fb, requested_vm)
    before injecting cache on the fallback body.

    This exercises chat_completions directly (with forward() stubbed to avoid
    any network I/O) rather than a bare apply_reasoning() unit call, since the
    bug lives in the fallback-body construction inside chat_completions, not
    in apply_reasoning itself (already covered by the tests above)."""
    monkeypatch.setitem(R.VIRTUAL_MODELS, "loxo/auto", VirtualModel(
        id="loxo/auto", cloud_target="z-ai/glm-5.2", routing="auto",
        vision="shim", reasoning="high"))
    monkeypatch.setattr(R, "_rate_cards_snapshot_and_maybe_refresh", lambda: {})

    captured = {}

    async def fake_forward(base_url, path, primary_body, headers, stream, **kw):
        captured["fallback_body"] = kw.get("fallback_body")
        return R.JSONResponse(content={"ok": True})
    monkeypatch.setattr(R, "forward", fake_forward)

    body = {"model": "loxo/auto", "messages": [{"role": "user", "content": "hi"}]}
    asyncio.run(R.chat_completions(
        _FakeChatRequest(body), x_quality=None, x_vision=None, authorization=None))

    assert captured["fallback_body"] is not None, "expected a cloud fallback to be built"
    fb = json.loads(captured["fallback_body"])
    assert fb["reasoning"] == {"effort": "high"}
