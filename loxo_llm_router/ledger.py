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


class SpendTracker:
    """In-memory cloud-spend totals + append-only JSONL persistence.

    Seeded from the ledger file at construction; a ledger read or write
    failure never breaks anything — cost is still counted in memory.
    """

    def __init__(self, ledger_path: pathlib.Path | None,
                 log: Callable[[str], None] = lambda _m: None):
        self.ledger_path = ledger_path
        self._log = log
        self._lock = asyncio.Lock()
        self._total_usd = 0.0
        self._requests = 0
        self._since = datetime.now(timezone.utc).isoformat()
        self._by_provider: dict[str, dict[str, Any]] = {}
        self._seed()

    def _accumulate(self, provider: str, model: str, usd: float) -> None:
        """Update totals (call with _lock held; _seed runs pre-loop, no lock needed)."""
        self._total_usd += usd
        self._requests += 1
        p = self._by_provider.setdefault(
            provider, {"total_usd": 0.0, "requests": 0, "by_model": {}})
        p["total_usd"] += usd
        p["requests"] += 1
        m = p["by_model"].setdefault(model, {"usd": 0.0, "requests": 0})
        m["usd"] += usd
        m["requests"] += 1

    def _seed(self) -> None:
        if not self.ledger_path or not self.ledger_path.exists():
            return
        earliest: str | None = None
        try:
            for raw in self.ledger_path.read_text().splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                usd = float(entry.get("usd", 0) or 0)
                if usd <= 0:
                    continue
                self._accumulate(entry.get("provider", "unknown"),
                                 entry.get("model", "unknown"), usd)
                ts = entry.get("ts", "")
                if ts and (earliest is None or ts < earliest):
                    earliest = ts
            if earliest:
                self._since = earliest
        except Exception as e:  # noqa: BLE001 - a bad ledger must not block startup
            self._log(f"[router] spend ledger seed failed ({e}); starting fresh")

    async def record(self, provider: str, model: str, usd: float,
                     stream: bool, reason: str) -> None:
        if usd <= 0:
            return
        async with self._lock:
            self._accumulate(provider, model, usd)
            total = self._total_usd
        self._log(f"[router] cloud cost=${usd:.6f} provider={provider} "
                  f"model={model} total=${total:.6f}")
        if not self.ledger_path:
            return
        entry = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "usd": usd,
            "stream": stream,
            "reason": reason,
        })
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a") as f:
                f.write(entry + "\n")
        except Exception as e:  # noqa: BLE001 - never break a response over the ledger
            self._log(f"[router] spend ledger write failed ({e}); "
                      f"cost still counted in memory")

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "total_usd": round(self._total_usd, 8),
                "requests": self._requests,
                "since": self._since,
                "by_provider": {
                    provider: {
                        "total_usd": round(pv["total_usd"], 8),
                        "requests": pv["requests"],
                        "by_model": {
                            m: {"usd": round(mv["usd"], 8), "requests": mv["requests"]}
                            for m, mv in sorted(pv["by_model"].items())
                        },
                    }
                    for provider, pv in sorted(self._by_provider.items())
                },
            }
