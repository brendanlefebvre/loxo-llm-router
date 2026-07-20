"""A1: heuristic request classification from observable shape (spec 2a).

Pure functions, no I/O. Ordered rules, first match wins; `unknown` is the
default — an inflated unknown rate is the classifier-health metric and must
stay visible rather than being laundered into `main`.

Fingerprints are grounded in captured OpenCode request bodies (2026-07-19
session), not guessed. OpenCode swaps its system prompt by target model
family, so `main` needs both variants. No compaction/summarize request has
been captured yet, so COMPACTION_FINGERPRINTS is empty and those requests
fall to `unknown` until a capture grounds the entry (bump CLASSIFIER_VERSION
when it does).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLASSIFIER_VERSION = 1

TITLE_FINGERPRINTS = (
    "You are a title generator",
)
MAIN_FINGERPRINTS = (
    "You are opencode, an interactive CLI tool",
    "You are OpenCode, the best coding agent on the planet",
)
COMPACTION_FINGERPRINTS: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    cls: str                  # "main" | "chore" | "compaction" | "unknown"
    signals: dict[str, Any]
    version: int


def _system_text(body: dict) -> str:
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return ""
    for m in msgs:
        if not (isinstance(m, dict) and m.get("role") == "system"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def classify(body: dict) -> Classification:
    msgs = body.get("messages")
    n_messages = len(msgs) if isinstance(msgs, list) else 0
    tools = body.get("tools")
    n_tools = len(tools) if isinstance(tools, list) else 0
    system = _system_text(body)
    signals: dict[str, Any] = {
        "messages": n_messages,
        "tools": n_tools,
        "stream": bool(body.get("stream", False)),
    }

    cls = "unknown"
    if any(fp in system for fp in TITLE_FINGERPRINTS) and n_tools == 0:
        cls = "chore"
    elif any(fp in system for fp in COMPACTION_FINGERPRINTS):
        cls = "compaction"
    elif any(fp in system for fp in MAIN_FINGERPRINTS) and n_tools > 0:
        cls = "main"

    return Classification(cls=cls, signals=signals, version=CLASSIFIER_VERSION)
