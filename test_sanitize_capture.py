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
    FILLER,
    NON_TEXT_CONTENT_PLACEHOLDER,
    _fill,
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


def test_fill_preserves_length_beyond_filler_size():
    """A message longer than FILLER (~18000 chars) must get filler of the
    SAME length, not truncated to len(FILLER) — the old `FILLER[:n]` slice
    violated the same-length contract for long messages."""
    n = len(FILLER) * 2 + 7
    out = _fill(n)
    assert len(out) == n
    assert out.startswith("The quick brown fox")


def test_system_content_list_with_non_text_part_fails_closed():
    """A system/developer message whose content is a list containing a
    non-text part (e.g. an image) must have that part replaced with the
    placeholder, not copied through unchanged."""
    body = {
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "You are opencode, an interactive CLI tool."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ]
    }
    out = sanitize(body)
    parts = out["messages"][0]["content"]
    assert parts[0]["text"] == "You are opencode, an interactive CLI tool."
    assert parts[1] == NON_TEXT_CONTENT_PLACEHOLDER
    assert "base64" not in json.dumps(out)


def test_system_text_part_drops_foreign_keys():
    """A text part in a system/developer message must be emitted as a clean
    {type, text} dict — foreign keys (cache_control, metadata, ...) must not
    survive, since they could carry operator content into a fixture."""
    body = {
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "You are opencode.",
                     "cache_control": {"ttl": "5m"}, "x_meta": "SECRET-PROJECT"},
                ],
            }
        ]
    }
    out = sanitize(body)
    part = out["messages"][0]["content"][0]
    assert set(part.keys()) == {"type", "text"}
    assert "SECRET-PROJECT" not in json.dumps(out)
    assert "cache_control" not in json.dumps(out)


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
