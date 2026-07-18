#!/usr/bin/env python3
"""Cache-affinity spike (v0.2.0 PR 1; ROADMAP risk 2).

Question: do prompt-cache discounts actually materialize through OpenRouter's
chat-completions dialect, and which injection mechanism wins?

  none    control - no cache_control anywhere
  auto    top-level `cache_control: {"type": "ephemeral"}` on the body
          (OpenRouter auto-advancing breakpoints on supported targets)
  manual  per-content-block breakpoints: system text block + the most recent
          assistant message (2 breakpoints, max allowed is 4)

Method: a multi-turn, OpenCode-shaped conversation (large stable system
prompt ~6k tokens + tool schemas + growing message list) sent DIRECTLY to
OpenRouter -- the router is deliberately not involved; the question is
OpenRouter+provider behavior. Turn N's prompt contains turn N-1's prompt as a
prefix, so cache reads should appear from turn 2 onward. A final duplicate of
the last request measures a pure re-read. All raw `usage` objects are
recorded verbatim: the findings doc reports what actually came back, not what
the docs promise.

Spends real money. Guard: --max-usd (default 1.00) aborts when the summed
`usage.cost` exceeds the budget.

Usage:
  OPENROUTER_API_KEY=... python3 scripts/cache_spike.py --mechanism all
  python3 scripts/cache_spike.py --mechanism manual --dry-run
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"  # cheap; strictest min-prefix class
MAX_COMPLETION_TOKENS = 64  # answers don't matter; the prompt side does

# ~6k tokens of deterministic, stable system prompt (~24k chars at ~4 chars/tok).
# Deliberately boring: byte-identical across turns and runs, like a real
# harness's frozen system prompt.
SYSTEM_TEXT = "You are a coding agent operating under the following house rules.\n" + "\n".join(
    f"Rule {i}: When working on subsystem {i}, always consult the design document "
    f"revision {i * 7} before editing, run the verification suite tagged v{i}, and "
    f"record the outcome in the engineering log under section {i}. Never skip the "
    f"review checklist items {i}a through {i}f, and escalate to the maintainer if "
    f"check {i}b fails twice in a row on the same artifact."
    for i in range(1, 401)
)

# OpenCode-shaped tool schemas: realistic size and structure, deterministic order.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run"},
                    "timeout_ms": {"type": "integer", "description": "Kill after this many ms"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "offset": {"type": "integer", "description": "First line to read"},
                    "limit": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file with a new string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
]

USER_TURNS = [
    "Summarize rule 42 in one sentence.",
    "Which rules mention a verification suite? Name three.",
    "Draft a one-line commit message for a fix to subsystem 7.",
    "What does rule 100 require before editing?",
]


def build_body(mechanism: str, messages: list[dict]) -> dict:
    """Return a request body for the given mechanism. `messages` excludes the
    system message; it is prepended here so each mechanism can shape it."""
    if mechanism == "manual":
        system_msg = {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": SYSTEM_TEXT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        msgs = [system_msg] + copy.deepcopy(messages)
        # Second breakpoint: end of the stable conversation prefix = the most
        # recent assistant message (if any). Convert its string content to a
        # block list so it can carry cache_control.
        for m in reversed(msgs):
            if m["role"] == "assistant" and isinstance(m.get("content"), str):
                m["content"] = [
                    {
                        "type": "text",
                        "text": m["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                break
    else:
        msgs = [{"role": "system", "content": SYSTEM_TEXT}] + messages

    body = {
        "model": ARGS.model,
        "messages": msgs,
        "tools": TOOLS,
        "max_tokens": MAX_COMPLETION_TOKENS,
        "stream": False,
        "usage": {"include": True},  # ask OpenRouter to include cost in usage
    }
    if mechanism == "auto":
        body["cache_control"] = {"type": "ephemeral"}
    return body


def call(client: httpx.Client, body: dict) -> dict:
    r = client.post(
        OPENROUTER_URL,
        json=body,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
    )
    r.raise_for_status()
    return r.json()


def run_mechanism(mechanism: str, out) -> float:
    """Run one full conversation; return total observed USD cost."""
    print(f"\n=== mechanism: {mechanism} (model {ARGS.model}) ===")
    messages: list[dict] = []
    spent = 0.0
    with httpx.Client(timeout=120.0) as client:
        calls = [("turn", i, USER_TURNS[i]) for i in range(ARGS.turns)]
        calls.append(("repeat-final", ARGS.turns - 1, USER_TURNS[ARGS.turns - 1]))
        for label, i, user_text in calls:
            if label == "turn":
                messages.append({"role": "user", "content": user_text})
            body = build_body(mechanism, messages)
            resp = call(client, body)
            usage = resp.get("usage", {}) or {}
            cost = float(usage.get("cost") or 0.0)
            spent += cost
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            print(
                f"  {label} {i}: prompt_tokens={usage.get('prompt_tokens')} "
                f"cached_tokens={cached} cost=${cost:.6f}"
            )
            out.write(json.dumps({
                "mechanism": mechanism, "label": label, "turn": i,
                "model": ARGS.model, "usage": usage,
                "provider": resp.get("provider"),
            }) + "\n")
            out.flush()
            if label == "turn":
                content = resp["choices"][0]["message"].get("content") or "(empty)"
                messages.append({"role": "assistant", "content": content})
            if spent > ARGS.max_usd:
                sys.exit(f"ABORT: spent ${spent:.4f} > --max-usd {ARGS.max_usd}")
    print(f"  subtotal: ${spent:.6f}")
    return spent


def main() -> None:
    mechanisms = ["none", "auto", "manual"] if ARGS.mechanism == "all" else [ARGS.mechanism]
    if ARGS.dry_run:
        for mech in mechanisms:
            body = build_body(mech, [{"role": "user", "content": USER_TURNS[0]}])
            print(f"--- {mech}: top-level keys {sorted(body)} ---")
            print(json.dumps(body, default=str)[:600], "...\n")
        print(f"system prompt: {len(SYSTEM_TEXT)} chars (~{len(SYSTEM_TEXT)//4} tokens)")
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY is not set")
    total = 0.0
    with open(ARGS.out, "a") as out:
        for mech in mechanisms:
            total += run_mechanism(mech, out)
    print(f"\nTOTAL observed cost: ${total:.6f}  (results appended to {ARGS.out})")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mechanism", choices=["none", "auto", "manual", "all"], default="all")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--turns", type=int, default=4, choices=range(1, 5))
    p.add_argument("--max-usd", type=float, default=1.00)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", default="spike-results.jsonl")
    ARGS = p.parse_args()
    main()
