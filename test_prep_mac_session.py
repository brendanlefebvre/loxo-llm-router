"""Tests for the prep/mac-session branch additions: request capture and the
SSE usage extractor behind the B1-validation cache instrumentation.

This branch is throwaway (validation + capture only); these tests keep the
suite honest while it exists.
"""

import json

import loxo_llm_router as R


# --- _capture_request ---------------------------------------------------------

def test_capture_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", "")
    R._capture_request(b'{"model": "x"}')  # must not raise, must write nothing
    # tmp_path also holds conftest's _empty_cwd/_empty_xdg — check captures only.
    assert not list(tmp_path.rglob("req-*.json"))


def test_capture_writes_raw_body(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(tmp_path / "caps"))
    raw = b'{"model": "loxo/auto", "messages": []}'
    R._capture_request(raw)
    files = list((tmp_path / "caps").glob("req-*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == raw  # byte-exact, pre-rewrite


def test_capture_failure_never_raises(tmp_path, monkeypatch):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("occupied")  # mkdir under a file path fails
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(blocked / "caps"))
    R._capture_request(b"{}")  # must not raise


# --- _extract_usage_from_sse_line --------------------------------------------

def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode()


def test_usage_extractor_returns_terminal_usage():
    u = R._extract_usage_from_sse_line(_sse({"usage": {
        "prompt_tokens": 10, "cost": 0.001,
        "prompt_tokens_details": {"cached_tokens": 8},
    }}))
    assert u is not None
    assert u["prompt_tokens_details"]["cached_tokens"] == 8


def test_usage_extractor_skips_null_and_empty_usage():
    assert R._extract_usage_from_sse_line(_sse({"usage": None})) is None
    assert R._extract_usage_from_sse_line(_sse({"usage": {}})) is None
    assert R._extract_usage_from_sse_line(_sse({"choices": []})) is None
    assert R._extract_usage_from_sse_line(b"data: [DONE]") is None
    assert R._extract_usage_from_sse_line(b"not sse") is None


def test_cost_extractor_still_works_via_usage():
    assert R._extract_cost_from_sse_line(_sse({"usage": {"cost": 0.5}})) == 0.5
    assert R._extract_cost_from_sse_line(_sse({"usage": {"prompt_tokens": 1}})) is None
