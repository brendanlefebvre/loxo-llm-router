"""Tests for LOXO_CAPTURE_DIR: opt-in raw request capture (fixture tooling)."""

import os
import stat

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


def test_capture_writes_owner_only_perms(tmp_path, monkeypatch):
    """Captured bodies contain full raw operator prompts; the file must be
    owner-only (0600), never inherit the umask's world/group-readable bits."""
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(tmp_path / "caps"))
    R._capture_request(b'{"model": "loxo/auto", "messages": []}')
    files = list((tmp_path / "caps").glob("req-*.json"))
    assert len(files) == 1
    mode = stat.S_IMODE(os.stat(files[0]).st_mode)
    assert mode == 0o600


def test_capture_skipped_for_replay(tmp_path, monkeypatch):
    """Replay traffic (X-Loxo-Replay) is never captured, so a replay run against
    a capture-enabled server can't feed on its own output (stream:false/temp:0.0
    artifacts). The endpoint passes replay=bool(x_loxo_replay) to this call."""
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(tmp_path / "caps"))
    R._capture_request(b'{"model": "loxo/deep", "stream": false, "temperature": 0.0}', replay=True)
    assert not list((tmp_path / "caps").rglob("req-*.json"))


def test_capture_writes_when_not_replay(tmp_path, monkeypatch):
    """Guard is off by default: ordinary (non-replay) traffic is still captured."""
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(tmp_path / "caps"))
    R._capture_request(b'{"model": "loxo/auto", "messages": []}', replay=False)
    assert len(list((tmp_path / "caps").glob("req-*.json"))) == 1


def test_capture_failure_never_raises(tmp_path, monkeypatch):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("occupied")
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(blocked / "caps"))
    R._capture_request(b"{}")  # must not raise
