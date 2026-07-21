"""Adversarial unit tests for scripts/sanitize_capture.py, targeted directly
at the sanitizer functions (not via the real fixtures) so a future harness
capture with a hostile shape gets caught before it's ever regenerated into a
committed golden fixture.

These are written to FAIL against the pre-hardening behavior and PASS
against the fix: a home path containing a space, a bare-dict content shape,
an indented/lowercased instructions marker, a None content, and tool_calls
argument redaction.
"""

import json

from scripts.sanitize_capture import (
    _redact_home_paths,
    _sanitize_content,
    _scrub_system_content,
    sanitize,
)


def test_home_path_with_space_fully_redacted():
    """A directory name with a space (e.g. "Old Projects") must not survive
    past the first space — the old regex `[^\\s\"\\\\]+` stopped there and
    leaked the remainder."""
    text = 'see /Users/bob/Old Projects/secret-thing for details'
    out = _redact_home_paths(text)
    assert "secret-thing" not in out
    assert "Old Projects" not in out
    assert "bob" not in out
    assert out.startswith("/Users/redacted") or "/Users/redacted" in out


def test_home_path_with_space_stops_at_json_boundary():
    """Redaction must still stop at a real newline boundary rather than
    consuming the entire (decoded) string — over-redacting to end-of-line
    is fine, swallowing subsequent lines is not."""
    text = "line one /Users/bob/My Documents/file.txt\nline two untouched"
    out = _redact_home_paths(text)
    assert "line two untouched" in out
    assert "My Documents" not in out
    assert "file.txt" not in out


def test_bare_dict_content_is_placeholdered_not_passed_through():
    """A content shape the sanitizer doesn't recognize (bare dict instead of
    str/list) must fail CLOSED — replaced with a placeholder, never passed
    through verbatim."""
    secret = {"type": "text", "text": "super secret operator data"}
    out = _sanitize_content(secret)
    assert out != secret
    assert "super secret operator data" not in json.dumps(out)
    assert isinstance(out, str)


def test_bare_dict_system_content_is_placeholdered_not_passed_through():
    secret = {"type": "text", "text": "super secret operator data"}
    out = _scrub_system_content(secret)
    assert out != secret
    assert "super secret operator data" not in json.dumps(out)


def test_none_content_passes_through_as_none():
    """None content carries no text and is valid — it must survive as None,
    not become a placeholder string."""
    assert _sanitize_content(None) is None
    assert _scrub_system_content(None) is None


def test_indented_lowercase_instructions_marker_is_stripped():
    """The marker must be tolerant of leading whitespace and case, not just
    the exact literal '^Instructions from:'."""
    text = (
        "You are opencode, an interactive CLI tool.\n"
        "    instructions loaded from: /Users/bob/proj/AGENTS.md\n"
        "Secret operator-authored project rules go here.\n"
        "Even more private content on another line.\n"
    )
    body = {"messages": [{"role": "system", "content": text}]}
    out = sanitize(body)
    scrubbed = out["messages"][0]["content"]
    assert "Secret operator-authored" not in scrubbed
    assert "private content" not in scrubbed
    assert "You are opencode, an interactive CLI tool." in scrubbed
    assert "[redacted operator instructions]" in scrubbed


def test_tool_calls_arguments_become_sanitized_len():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": "cat ~/.ssh/id_rsa"}),
                        },
                    }
                ],
            }
        ]
    }
    out = sanitize(body)
    tc = out["messages"][0]["tool_calls"][0]
    args = json.loads(tc["function"]["arguments"])
    assert set(args.keys()) == {"sanitized_len"}
    assert isinstance(args["sanitized_len"], int)
    assert "id_rsa" not in json.dumps(out)
