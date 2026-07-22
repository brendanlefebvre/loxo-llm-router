#!/usr/bin/env python3
"""Sanitize a captured request body into a committable golden fixture.

Only the harness's own IDENTITY/FINGERPRINT text is preserved verbatim in
`system`/`developer` messages — the substrings the classifier matches on
(e.g. "You are opencode, an interactive CLI tool...") plus the surrounding
tool-inventory prose. Content the harness injects about the OPERATOR's
session — the `<env>...</env>` block (cwd, workspace root, platform, date)
and any operator-authored instructions block (marked by a line starting
`Instructions from:`, e.g. an AGENTS.md/CLAUDE.md inclusion) — is stripped
and replaced with placeholders, since it has zero classification value and
leaks private machine/project details.

Everything the OPERATOR typed or the models produced (user/assistant/tool
content, tool-call arguments) is replaced with deterministic filler of the
same length, so byte-size-dependent behavior (token estimation) stays
realistic while zero original content survives.

After the per-message pass, two more redaction passes run over the ENTIRE
output body (messages, tools array, top-level params — every string value
at any depth):
  1. Machine/user-private absolute-path roots are collapsed to a placeholder,
     catching paths embedded in the tools array or in env lines the
     per-message scrub didn't fully cover:
       - `/Users/...`            -> `/Users/redacted`
       - `/home/...`             -> `/home/redacted`
       - `/var/folders/...`      -> `/var/folders/redacted`
       - `/private/var/folders/...`, `/private/tmp/...`
                                 -> `/private/redacted`
       - `/tmp/<opaque-segment>...` (hash/random-looking first segment)
                                 -> `/tmp/redacted`
     Generic shared install paths (`/opt/homebrew/...`, `/usr/...`, `/opt/...`
     node_modules trees) are deliberately left alone — identical across
     installs, carry no operator-unique data, and leaving them keeps fixture
     realism.
  2. Operator name tokens (from --redact / LOXO_SANITIZE_REDACT, never
     hardcoded here) are replaced case-insensitively with "redacted".

Usage: python3 scripts/sanitize_capture.py IN.json OUT.json [--redact NAME ...]
       LOXO_SANITIZE_REDACT="brendanl,brendan" python3 scripts/sanitize_capture.py IN.json OUT.json
"""

from __future__ import annotations

import json
import os
import re
import sys

FILLER = ("The quick brown fox jumps over the lazy dog. " * 400)

ENV_BLOCK_RE = re.compile(r"<env>.*?</env>", re.DOTALL)
ENV_PLACEHOLDER = "<env>[redacted session environment]</env>"

INSTRUCTIONS_MARKER_RE = re.compile(
    r"^\s*instructions\s+(?:from|loaded from):", re.IGNORECASE | re.MULTILINE
)
INSTRUCTIONS_PLACEHOLDER = "[redacted operator instructions]"

NON_TEXT_CONTENT_PLACEHOLDER = "[non-text content removed]"

# Greedy to a JSON-string-safe boundary (closing quote, newline, or escape),
# NOT just whitespace — a path containing a space (e.g. a directory named
# "My Documents") must be redacted in full rather than leaving the tail
# after the first space to leak. Over-redacting to end-of-line in prose is
# safe (no leak); under-redacting at a space is not.
HOME_PATH_RE = re.compile(r"/(Users|home)/[^\"\n\\]*")

# macOS per-user private-tmp roots. `/private/var/folders/...` and
# `/private/tmp/...` are matched BEFORE the bare `/var/folders/...` rule
# below, since the latter's literal also appears as a substring of the
# former — once the `/private/...` rule fires, that substring is gone by
# the time the bare rule runs, so a single left-to-right rule list (private
# first) is all that's needed rather than overlap-aware matching.
PRIVATE_TMP_RE = re.compile(r"/private/(?:var/folders|tmp)[^\"\n\\]*")

# `/var/folders/<xx>/<DARWIN_USER_TEMP_DIR token>/...` — the per-user
# bootstrap token is a stable machine fingerprint (this is exactly I-1).
VAR_FOLDERS_RE = re.compile(r"/var/folders/[^\"\n\\]*")

# `/tmp/<segment>...` — only redact when the first path segment looks
# opaque/hash-like (mktemp-style random tokens, hex hashes, uuid-ish dirs),
# not a plain word like `/tmp/opencode`. Heuristic: the first segment
# contains at least one digit and is at least 6 characters — random tokens
# reliably trip this, ordinary words usually don't. Prefer over-redaction.
TMP_SEGMENT_RE = re.compile(r"/tmp/([^/\"\n\\]+)[^\"\n\\]*")


def _looks_opaque(segment: str) -> bool:
    return len(segment) >= 6 and re.search(r"\d", segment) is not None


def _redact_tmp_segment(text: str) -> str:
    def _repl(m: "re.Match[str]") -> str:
        return "/tmp/redacted" if _looks_opaque(m.group(1)) else m.group(0)
    return TMP_SEGMENT_RE.sub(_repl, text)


def _redact_paths_in_string(text: str) -> str:
    """Apply all machine/user-private path redaction rules to one string,
    in an order where more-specific rules run first so a generic rule can't
    steal a substring a specific rule should have claimed whole (see
    PRIVATE_TMP_RE vs VAR_FOLDERS_RE above)."""
    text = PRIVATE_TMP_RE.sub("/private/redacted", text)
    text = VAR_FOLDERS_RE.sub("/var/folders/redacted", text)
    text = _redact_tmp_segment(text)
    text = HOME_PATH_RE.sub(lambda m: f"/{m.group(1)}/redacted", text)
    return text


def _fill(n: int) -> str:
    n = max(n, 1)
    return (FILLER * (n // len(FILLER) + 1))[:n]


def _sanitize_content(content):
    if content is None:
        return None
    if isinstance(content, str):
        return _fill(len(content))
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append({"type": "text", "text": _fill(len(part.get("text", "")))})
            else:
                out.append({"type": "text", "text": "[non-text part removed]"})
        return out
    # Unrecognized shape (bare dict, int, ...): fail CLOSED rather than
    # passing unredacted content through into a committed fixture.
    return NON_TEXT_CONTENT_PLACEHOLDER


def _scrub_operator_text(text: str) -> str:
    """Strip injected <env> blocks and operator-authored instructions from
    harness-owned (system/developer) text, leaving identity/fingerprint
    prose and tool-inventory description intact."""
    text = ENV_BLOCK_RE.sub(ENV_PLACEHOLDER, text)
    # Truncate at the first "Instructions from:" line — everything from
    # that marker to the end of the message is operator-authored.
    m = INSTRUCTIONS_MARKER_RE.search(text)
    if m:
        text = text[: m.start()] + INSTRUCTIONS_PLACEHOLDER
    return text


def _scrub_system_content(content):
    if content is None:
        return None
    if isinstance(content, str):
        return _scrub_operator_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                # Emit a clean text part — never spread `**part`, which would
                # preserve foreign keys (cache_control, metadata, ...) that
                # could carry operator content into a committed fixture.
                out.append({"type": "text", "text": _scrub_operator_text(part.get("text", ""))})
            else:
                out.append(NON_TEXT_CONTENT_PLACEHOLDER)
        return out
    # Unrecognized shape (bare dict, int, ...): fail CLOSED rather than
    # passing unredacted content through into a committed fixture.
    return NON_TEXT_CONTENT_PLACEHOLDER


def sanitize(body: dict) -> dict:
    out = dict(body)
    msgs = []
    for m in body.get("messages", []):
        if not isinstance(m, dict):
            continue
        if m.get("role") in ("system", "developer"):
            nm = dict(m)
            nm["content"] = _scrub_system_content(m.get("content"))
            msgs.append(nm)  # harness-owned, identity text kept verbatim
            continue
        nm = {"role": m.get("role", "user"),
              "content": _sanitize_content(m.get("content", ""))}
        if isinstance(m.get("tool_calls"), list):
            nm["tool_calls"] = [
                {"id": tc.get("id", f"call_{i}"), "type": "function",
                 "function": {"name": (tc.get("function") or {}).get("name", "tool"),
                              "arguments": json.dumps({"sanitized_len":
                                  len((tc.get("function") or {}).get("arguments", ""))})}}
                for i, tc in enumerate(m["tool_calls"]) if isinstance(tc, dict)
            ]
        if "tool_call_id" in m:
            nm["tool_call_id"] = m["tool_call_id"]
        msgs.append(nm)
    out["messages"] = msgs
    return out


# Name kept as `_redact_home_paths` for API stability (test_sanitize_capture.py
# imports it directly); it now recurses `_redact_paths_in_string`, which
# covers home paths plus the other machine/user-private path families above.
def _redact_home_paths(value):
    if isinstance(value, str):
        return _redact_paths_in_string(value)
    if isinstance(value, list):
        return [_redact_home_paths(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_home_paths(v) for k, v in value.items()}
    return value


def _redact_names(value, names: list[str]):
    if not names:
        return value
    if isinstance(value, str):
        out = value
        for name in names:
            if not name:
                continue
            out = re.sub(re.escape(name), "redacted", out, flags=re.IGNORECASE)
        return out
    if isinstance(value, list):
        return [_redact_names(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _redact_names(v, names) for k, v in value.items()}
    return value


def _collect_redact_names(argv: list[str]) -> tuple[list[str], list[str]]:
    """Pull --redact NAME (repeatable) out of argv; merge with
    LOXO_SANITIZE_REDACT env var (comma-separated). Returns (names, rest_argv)."""
    names: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--redact":
            if i + 1 >= len(argv):
                sys.exit("error: --redact requires a NAME argument")
            names.append(argv[i + 1])
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    env_names = os.environ.get("LOXO_SANITIZE_REDACT", "")
    names.extend(n.strip() for n in env_names.split(",") if n.strip())
    return names, rest


if __name__ == "__main__":
    redact_names, rest = _collect_redact_names(sys.argv[1:])
    src, dst = rest[0], rest[1]
    with open(src) as f:
        body = json.load(f)
    sanitized = sanitize(body)
    sanitized = _redact_home_paths(sanitized)
    sanitized = _redact_names(sanitized, redact_names)
    with open(dst, "w") as f:
        json.dump(sanitized, f, indent=1)
        f.write("\n")
