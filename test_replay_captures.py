"""Tests for scripts/replay_captures.py: auth, resume semantics, body hygiene."""

import json

from scripts import replay_captures as rc


# --- auth ---------------------------------------------------------------------

def test_build_request_carries_bearer_token():
    """Against a ROUTER_TOKEN-enabled router an unauthenticated replay 401s on
    every capture in seconds and the run looks 'complete' with zero data."""
    req = rc._build_request("http://x/v1/chat/completions", {"a": 1}, token="sekrit")
    assert req.headers["Authorization"] == "Bearer sekrit"


def test_build_request_without_token_sends_no_auth_header():
    req = rc._build_request("http://x/v1/chat/completions", {"a": 1}, token="")
    assert "Authorization" not in req.headers


def test_build_request_always_marks_replay():
    req = rc._build_request("http://x/v1/chat/completions", {}, token="")
    # urllib capitalizes header keys on add: "X-Loxo-Replay" -> "X-loxo-replay"
    assert req.headers["X-loxo-replay"] == "1"


# --- resume semantics ---------------------------------------------------------

def _write_rows(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_resume_keeps_terminal_rows_and_retries_transport_errors(tmp_path):
    """A transport error (server down, MLX mid-reload) means the tier never got
    to judge the request — that capture must be retried on resume, not counted
    done. An HTTP error (422 overflow, 500 OOM) is the server's verdict and is
    terminal. A row-level error is a deterministic capture-parse failure and is
    terminal. Retryable rows are dropped from the file so downstream analysis
    never sees two rows for one capture."""
    out = tmp_path / "r.jsonl"
    _write_rows(out, [
        {"capture": "a.json", "cls": "main", "local": {"ok": True}, "deep": {"ok": True}},
        {"capture": "b.json", "cls": "main",
         "local": {"ok": False, "error": "HTTP 422", "detail": "prompt too large"},
         "deep": {"ok": True}},
        {"capture": "c.json", "cls": "main",
         "local": {"ok": False, "error": "URLError", "detail": "connection refused"},
         "deep": {"ok": True}},
        {"capture": "d.json", "error": "ValueError", "detail": "capture root is list"},
        {"capture": "e.json", "cls": "main", "local": {"ok": True},
         "deep": {"ok": False, "error": "TimeoutError", "detail": ""}},
    ])
    done = rc._compact_for_resume(out)
    assert done == {"a.json", "b.json", "d.json"}
    kept = [json.loads(line)["capture"] for line in out.read_text().splitlines()]
    assert kept == ["a.json", "b.json", "d.json"]


def test_resume_drops_half_written_final_line(tmp_path):
    out = tmp_path / "r.jsonl"
    out.write_text(
        json.dumps({"capture": "a.json", "cls": "main",
                    "local": {"ok": True}, "deep": {"ok": True}}) + "\n"
        + '{"capture": "trunc'  # crash mid-write
    )
    done = rc._compact_for_resume(out)
    assert done == {"a.json"}
    assert [json.loads(line)["capture"] for line in out.read_text().splitlines()] == ["a.json"]


def test_resume_missing_file_is_empty(tmp_path):
    assert rc._compact_for_resume(tmp_path / "absent.jsonl") == set()


def test_resume_skips_valid_json_rows_of_wrong_shape(tmp_path):
    """A row that parses as JSON but isn't a dict (or lacks a usable 'capture')
    must be dropped for retry, not crash resume — one such line previously
    aborted the whole run with AttributeError/KeyError."""
    out = tmp_path / "r.jsonl"
    good = {"capture": "a.json", "cls": "main", "local": {"ok": True}, "deep": {"ok": True}}
    out.write_text(
        json.dumps(good) + "\n"
        + "[1, 2, 3]\n"                                   # valid JSON, not a dict
        + "42\n"                                          # valid JSON scalar
        + json.dumps({"cls": "main", "error": "x"}) + "\n"  # dict without capture
        + json.dumps({"capture": "", "error": "x"}) + "\n"  # empty capture name
    )
    done = rc._compact_for_resume(out)
    assert done == {"a.json"}
    assert [json.loads(line)["capture"] for line in out.read_text().splitlines()] == ["a.json"]


# --- replay body hygiene ------------------------------------------------------

def test_replay_one_drops_stream_options(monkeypatch):
    """stream:false + stream_options is OpenAI-spec-invalid; a strict backend's
    400 would be misread as a tier failure rather than a harness artifact."""
    sent_bodies = []

    def fake_post(url, body, timeout, token=""):
        sent_bodies.append(body)
        return 0.1, {"ok": True, "response": {"choices": [{}], "model": "m"}}

    monkeypatch.setattr(rc, "_post", fake_post)
    body = {"model": "x", "stream": True,
            "stream_options": {"include_usage": True}, "messages": []}
    rc._replay_one(body, "http://x", 0.0, False, 5.0, token="")
    assert len(sent_bodies) == 2  # local + deep
    for sent in sent_bodies:
        assert sent["stream"] is False
        assert "stream_options" not in sent
    assert "stream_options" in body  # caller's dict untouched


def test_replay_one_passes_token_to_post(monkeypatch):
    seen_tokens = []

    def fake_post(url, body, timeout, token=""):
        seen_tokens.append(token)
        return 0.1, {"ok": True, "response": {"choices": [{}], "model": "m"}}

    monkeypatch.setattr(rc, "_post", fake_post)
    rc._replay_one({"model": "x", "messages": []}, "http://x", 0.0, False, 5.0, token="sekrit")
    assert seen_tokens == ["sekrit", "sekrit"]
