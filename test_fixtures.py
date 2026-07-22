"""Golden-fixture replay: sanitized real OpenCode request bodies, pinned
2026-07 (see scripts/sanitize_capture.py for what sanitization preserves).
When a harness update drifts its request shapes, these fail before a live
session does."""

import json
import pathlib
import re

import pytest

from loxo_llm_router.classify import CLASSIFIER_VERSION, classify

FIXROOT = pathlib.Path(__file__).parent / "tests" / "fixtures"

EXPECTED = {
    "opencode-2026-07/title-chore.json": "chore",
    "opencode-2026-07/main-first-turn.json": "main",
    "opencode-2026-07/main-tool-heavy.json": "main",
    "opencode-2026-07/main-anthropic-variant.json": "main",
    "opencode-2026-07/main-late-session.json": "main",
    "opencode-2026-07/compaction.json": "compaction",
    "pi-2026-07/main-first-turn.json": "main",
    "pi-2026-07/main-developer-role.json": "main",
    "pi-2026-07/main-local-small.json": "main",
    "pi-2026-07/compaction.json": "compaction",
}


def _load(name):
    return json.loads((FIXROOT / name).read_text())


@pytest.mark.parametrize("name,expected", sorted(EXPECTED.items()))
def test_fixture_classifies(name, expected):
    c = classify(_load(name))
    assert c.cls == expected, f"{name}: got {c.cls}, signals {c.signals}"
    assert c.version == CLASSIFIER_VERSION


def test_fixtures_carry_no_original_content():
    """Sanitization invariant: non-instruction message text is filler only."""
    filler_start = "The quick brown fox"
    for name in EXPECTED:
        for m in _load(name).get("messages", []):
            if m.get("role") in ("system", "developer"):
                continue
            c = m.get("content")
            if c is None:
                texts = []
            elif isinstance(c, str):
                texts = [c]
            else:
                texts = [p.get("text", "") for p in c if isinstance(p, dict)]
            for t in texts:
                # t may be shorter than filler_start (original content was very
                # short, so the filler was truncated) — check the prefix
                # relationship in both directions.
                assert (t == "" or t.startswith(filler_start)
                        or filler_start.startswith(t)
                        or t == "[non-text part removed]"), \
                    f"unsanitized content in {name}"


def test_fixtures_scrub_operator_environment():
    """Sanitization invariant: injected <env> blocks and operator-authored
    AGENTS.md/CLAUDE.md instruction text must not survive into a committed
    fixture, and no home path or other machine/user-private absolute path
    (macOS per-user temp dirs included) other than the sanctioned
    placeholder may appear anywhere in the fixture body (messages, tools,
    top-level).

    Deliberately does NOT reuse the sanitizer's own redaction regexes — a
    shared regex can't catch a regression in itself. Instead this asserts
    independent invariants, one per path family, using assertions written
    from scratch against the JSON-serialized fixture blob."""
    for name in EXPECTED:
        blob = json.dumps(_load(name))
        assert "Instructions from:" not in blob, \
            f"operator instructions marker leaked in {name}"
        assert "Working directory:" not in blob, \
            f"unredacted env line leaked in {name}"
        assert "Workspace root folder:" not in blob, \
            f"unredacted env line leaked in {name}"

        # Every occurrence of the home-path prefix must be the placeholder.
        for m in re.finditer(r"/(Users|home)/", blob):
            tail = blob[m.end():]
            assert tail.startswith("redacted"), \
                f"unredacted home path in {name}: {blob[m.start():m.start()+40]!r}"
        # Nothing but a clean boundary may follow the placeholder itself.
        assert re.search(r'/(?:Users|home)/redacted[^\n"\\]', blob) is None, \
            f"trailing leak after home-path placeholder in {name}"

        # macOS per-user private-tmp roots (I-1: the DARWIN_USER_TEMP_DIR
        # bootstrap token is a stable machine fingerprint). No survivor
        # other than the placeholder is acceptable anywhere in the blob.
        var_folders_hits = re.findall(r'/var/folders/[^\s"\\]*', blob)
        assert var_folders_hits == [] or set(var_folders_hits) == {"/var/folders/redacted"}, \
            f"unredacted /var/folders path leaked in {name}: {var_folders_hits}"
        private_hits = re.findall(r'/private/[^\s"\\]*', blob)
        assert private_hits == [] or set(private_hits) == {"/private/redacted"}, \
            f"unredacted /private path leaked in {name}: {private_hits}"
        # The literal per-user bootstrap token from I-1 must never appear,
        # regardless of which path family it's embedded in.
        assert "qs2_r1y56" not in blob, \
            f"operator DARWIN_USER_TEMP_DIR token leaked in {name}"


def test_fixture_shapes_are_stable():
    """Pin the shape signals the classifier depends on."""
    t = _load("opencode-2026-07/title-chore.json")
    assert not t.get("tools")
    m = _load("opencode-2026-07/main-tool-heavy.json")
    assert len(m.get("tools", [])) == 11
    assert len(_load("pi-2026-07/main-first-turn.json").get("tools", [])) == 4
    assert not _load("opencode-2026-07/compaction.json").get("tools")
    assert not _load("pi-2026-07/compaction.json").get("tools")


def test_pi_reasoning_param_preserved():
    """Pi sends OpenRouter-shape reasoning (client-wins path, B2): the
    fixture must retain it so replay covers the pass-through branch."""
    b = _load("pi-2026-07/main-developer-role.json")
    assert b.get("reasoning") == {"effort": "medium"}
