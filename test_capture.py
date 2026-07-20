"""Tests for LOXO_CAPTURE_DIR: opt-in raw request capture (fixture tooling)."""

import loxo_llm_router as R


def test_capture_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", "")
    R._capture_request(b'{"model": "x"}')  # must not raise, must write nothing
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
    blocked.write_text("occupied")
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(blocked / "caps"))
    R._capture_request(b"{}")  # must not raise
