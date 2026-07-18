"""Operational state: the LOXO_STATE_DIR root and the spend ledger.

State is separated from configuration per the XDG split (ROADMAP,
"State and ledgers"): config may be read-only and backed up; ledgers are
append-only operational history. Files under the state root are never
rewritten in place — rotation is allowed, mutation is not.

Resolution:
  state root    LOXO_STATE_DIR > $XDG_STATE_HOME/loxo-llm-router
                > ~/.local/state/loxo-llm-router
  spend ledger  SPEND_LEDGER ("" disables) > <state root>/spend.jsonl
                > legacy ~/.config/loxo-llm-router/spend.jsonl (only while it
                exists and the new path doesn't; a pointer is logged rather
                than forking history across two files)
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Callable

LEGACY_SPEND_LEDGER = pathlib.Path.home() / ".config" / "loxo-llm-router" / "spend.jsonl"


def state_dir() -> pathlib.Path:
    env = os.environ.get("LOXO_STATE_DIR")
    if env:
        return pathlib.Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "state"
    return base / "loxo-llm-router"


def resolve_spend_ledger(log: Callable[[str], None] = lambda _m: None) -> pathlib.Path | None:
    env = os.environ.get("SPEND_LEDGER")
    if env is not None:
        return pathlib.Path(env) if env else None
    new = state_dir() / "spend.jsonl"
    if not new.exists() and LEGACY_SPEND_LEDGER.exists():
        log(f"[router] spend ledger at legacy path {LEGACY_SPEND_LEDGER}; "
            f"move it to {new} to complete the state-dir migration")
        return LEGACY_SPEND_LEDGER
    return new
