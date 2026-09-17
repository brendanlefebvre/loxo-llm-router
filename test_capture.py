"""Tests for LOXO_CAPTURE_DIR: opt-in raw request capture (fixture tooling)."""

import asyncio
import json
import os
import stat

import loxo_llm_router as R


class _FakeChatRequest:
    """Minimal stand-in for fastapi.Request: chat_completions only awaits
    .body() and reads .headers."""
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()
        self.headers: dict[str, str] = {}

    async def body(self):
        return self._body


def _chat(req, monkeypatch, capture_obs: dict, **headers):
    """Drive chat_completions with forward() stubbed; record the obs it gets."""
    async def fake_forward(base_url, path, primary_body, hdrs, stream, **kw):
        capture_obs["obs"] = kw.get("obs")
        return R.JSONResponse(content={"ok": True})
    monkeypatch.setattr(R, "forward", fake_forward)
    kwargs = dict(x_loxo_quality=None, x_loxo_vision=None, x_loxo_session_id=None,
                  x_loxo_replay=None, x_opencode_class=None, authorization=None)
    kwargs.update(headers)
    asyncio.run(R.chat_completions(req, **kwargs))


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


# --- X-Loxo-Replay header parsing --------------------------------------------

def test_replay_header_falsey_values_are_not_replay():
    """bool(header) treated 'X-Loxo-Replay: 0' as replay-true, silently dropping
    organic traffic from the capture corpus. Explicit false spellings, empty
    strings, and non-string shapes (FastAPI Header defaults in direct calls)
    must all read as NOT a replay."""
    for v in (None, "", "   ", "0", "false", "FALSE", "no", "off", object()):
        assert R._replay_requested(v) is False, repr(v)


def test_replay_header_truthy_values_are_replay():
    for v in ("1", "true", "TRUE", "yes", "on", " 1 "):
        assert R._replay_requested(v) is True, repr(v)


def test_endpoint_captures_when_replay_header_says_false(tmp_path, monkeypatch):
    """A proxy stamping 'X-Loxo-Replay: 0' on organic traffic must not disable
    capture."""
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", str(tmp_path / "caps"))
    req = _FakeChatRequest({"model": "loxo/deep", "messages": []})
    _chat(req, monkeypatch, {}, x_loxo_replay="0")
    assert len(list((tmp_path / "caps").glob("req-*.json"))) == 1


# --- replay traffic and the adequacy ledger -----------------------------------

def test_replay_request_creates_no_observation(monkeypatch):
    """The self-feed guard must cover the ledger sink too: replay traffic is
    synthetic (forced-tier, stream:false) and must not become dial evidence.
    No Observation at all -> nothing for record() to fan out."""
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", "")
    captured = {}
    req = _FakeChatRequest({"model": "loxo/deep", "messages": []})
    _chat(req, monkeypatch, captured, x_loxo_replay="1")
    assert captured["obs"] is None


def test_organic_request_still_creates_observation(monkeypatch):
    monkeypatch.setattr(R, "LOXO_CAPTURE_DIR", "")
    captured = {}
    req = _FakeChatRequest({"model": "loxo/deep", "messages": []})
    _chat(req, monkeypatch, captured)
    assert captured["obs"] is not None
