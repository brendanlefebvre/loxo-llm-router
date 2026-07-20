"""Tests for A2: Observation record, StreamScan signal collector, AdequacyLedger."""

import asyncio
import json

import pytest

from loxo_llm_router import ledger


def _obs(**kw):
    base = dict(cls="main", classifier_version=1, requested_model="loxo/auto",
                route="cloud", served_model="z-ai/glm-5.2", reason="virtual-quality-best",
                stream=True)
    base.update(kw)
    return ledger.Observation(**base)


# --- Observation.to_entry -----------------------------------------------------

def test_entry_has_exact_spec_schema():
    o = _obs(status=200, latency_ms=1200, ttfb_ms=300, finish_reason="stop",
             had_tool_calls=True, tool_calls_valid_json=True, usd=0.01,
             usage={"prompt_tokens": 100, "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "completion_tokens_details": {"reasoning_tokens": 5}})
    e = o.to_entry()
    assert set(e) == {"ts", "class", "classifier_version", "requested_model", "route",
                      "served_model", "reason", "stream", "status", "latency_ms",
                      "ttfb_ms", "fallback_fired", "finish_reason", "had_tool_calls",
                      "tool_calls_valid_json", "tokens", "usd", "shadow"}
    assert e["class"] == "main"
    assert e["shadow"] is False
    assert e["tokens"] == {"prompt": 100, "completion": 20, "reasoning": 5, "cached": 80}


def test_entry_tolerates_missing_usage():
    e = _obs(usage=None).to_entry()
    assert e["tokens"] == {"prompt": None, "completion": None, "reasoning": None, "cached": None}
    assert e["status"] is None


# --- StreamScan ---------------------------------------------------------------

def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode()


def test_scan_collects_finish_reason_and_usage():
    s = ledger.StreamScan()
    s.feed_line(_sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}))
    s.feed_line(_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    s.feed_line(_sse({"usage": {"prompt_tokens": 9, "cost": 0.001}, "choices": []}))
    s.feed_line(b"data: [DONE]")
    assert s.finish_reason == "stop"
    assert s.usage == {"prompt_tokens": 9, "cost": 0.001}
    assert s.had_tool_calls is False
    assert s.tool_calls_valid() is None  # no tool calls -> no verdict


def test_scan_assembles_and_validates_tool_call_fragments():
    s = ledger.StreamScan()
    s.feed_line(_sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "bash", "arguments": "{\"comm"}}]}}]}))
    s.feed_line(_sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "and\": \"ls\"}"}}]}}]}))
    assert s.had_tool_calls is True
    assert s.tool_calls_valid() is True


def test_scan_flags_invalid_tool_json():
    s = ledger.StreamScan()
    s.feed_line(_sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{not json"}}]}}]}))
    assert s.tool_calls_valid() is False


def test_scan_survives_garbage_lines():
    s = ledger.StreamScan()
    s.feed_line(b"random noise")
    s.feed_line(b"data: {broken json")
    s.feed_line(_sse({"usage": None}))
    assert s.usage is None and s.finish_reason is None


def test_scan_from_nonstream_body():
    s = ledger.StreamScan.from_response_body({
        "choices": [{"message": {"content": "x", "tool_calls": [
            {"function": {"name": "bash", "arguments": "{\"a\": 1}"}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 4},
    })
    assert s.finish_reason == "tool_calls"
    assert s.had_tool_calls is True
    assert s.tool_calls_valid() is True
    assert s.usage == {"prompt_tokens": 4}


# --- resolve_adequacy_ledger / AdequacyLedger ---------------------------------

@pytest.fixture(autouse=True)
def state_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ADEQUACY_LEDGER", raising=False)
    monkeypatch.setenv("LOXO_STATE_DIR", str(tmp_path / "state"))
    yield


def test_resolve_defaults_under_state_dir(tmp_path):
    assert ledger.resolve_adequacy_ledger() == tmp_path / "state" / "adequacy.jsonl"


def test_resolve_env_override_and_disable(monkeypatch, tmp_path):
    monkeypatch.setenv("ADEQUACY_LEDGER", str(tmp_path / "x.jsonl"))
    assert ledger.resolve_adequacy_ledger() == tmp_path / "x.jsonl"
    monkeypatch.setenv("ADEQUACY_LEDGER", "")
    assert ledger.resolve_adequacy_ledger() is None


def test_adequacy_write_appends_entry(tmp_path):
    led = ledger.AdequacyLedger(tmp_path / "sub" / "adequacy.jsonl")
    asyncio.run(led.write(_obs(status=200)))
    asyncio.run(led.write(_obs(status=502, route="local", served_model="qwen")))
    lines = [json.loads(x) for x in (tmp_path / "sub" / "adequacy.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["status"] == 200 and lines[0]["shadow"] is False
    assert lines[1]["route"] == "local"


def test_adequacy_disabled_and_failure_never_raise(tmp_path):
    asyncio.run(ledger.AdequacyLedger(None).write(_obs()))          # disabled: no-op
    blocked = tmp_path / "as-dir"
    blocked.mkdir()
    asyncio.run(ledger.AdequacyLedger(blocked).write(_obs()))       # write fails: swallowed
