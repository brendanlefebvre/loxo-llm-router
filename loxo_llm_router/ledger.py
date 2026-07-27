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

    def _accumulate(self, provider: str, model: str, usd: float,
                     cached_tokens: int = 0, cache_savings_usd: float = 0.0) -> None:
        """Update totals (call with _lock held; _seed runs pre-loop, no lock needed)."""
        self._total_usd += usd
        self._requests += 1
        p = self._by_provider.setdefault(
            provider, {"total_usd": 0.0, "requests": 0, "by_model": {}})
        p["total_usd"] += usd
        p["requests"] += 1
        m = p["by_model"].setdefault(
            model, {"usd": 0.0, "requests": 0, "cached_tokens": 0, "est_cache_savings_usd": 0.0})
        m.setdefault("cached_tokens", 0)
        m.setdefault("est_cache_savings_usd", 0.0)
        m["usd"] += usd
        m["requests"] += 1
        m["cached_tokens"] += cached_tokens
        m["est_cache_savings_usd"] += cache_savings_usd

    def _seed(self) -> None:
        if not self.ledger_path or not self.ledger_path.exists():
            return
        earliest: str | None = None
        try:
            with self.ledger_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    usd = float(entry.get("usd", 0) or 0)
                    cached = int(entry.get("cached_tokens", 0) or 0)
                    savings = float(entry.get("cache_savings_usd", 0) or 0)
                    # Mirror record()'s guard: a true no-op (no cost, no cache
                    # activity) is skipped, but a zero-cost entry that still
                    # carries cache stats must still be re-accumulated on reload.
                    if usd <= 0 and cached == 0 and savings == 0:
                        continue
                    self._accumulate(entry.get("provider", "unknown"),
                                     entry.get("model", "unknown"), usd,
                                     cached_tokens=cached,
                                     cache_savings_usd=savings)
                    ts = entry.get("ts", "")
                    if ts and (earliest is None or ts < earliest):
                        earliest = ts
            if earliest:
                self._since = earliest
        except Exception as e:  # noqa: BLE001 - a bad ledger must not block startup
            self._log(f"[router] spend ledger seed failed ({e}); starting fresh")

    async def record(self, provider: str, model: str, usd: float,
                     stream: bool, reason: str,
                     cached_tokens: int = 0, cache_savings_usd: float = 0.0) -> None:
        # A no-cost request with no cache activity is a true no-op. But a
        # zero-cost request that still carries cache stats (e.g. a fully
        # cache-hit response) must still be recorded — usd contributes 0,
        # but requests/cached_tokens/est_cache_savings_usd must accumulate.
        if usd <= 0 and cached_tokens == 0 and cache_savings_usd == 0:
            return
        async with self._lock:
            self._accumulate(provider, model, usd,
                              cached_tokens=cached_tokens, cache_savings_usd=cache_savings_usd)
            total = self._total_usd
        self._log(f"[router] cloud cost=${usd:.6f} provider={provider} "
                  f"model={model} total=${total:.6f}")
        if not self.ledger_path:
            return
        # Append outside the lock on purpose: O_APPEND keeps concurrent writes
        # byte-safe, and blocking file IO must not serialize the accumulator;
        # file order may diverge from accumulation order. The write itself
        # runs in a worker thread so it never blocks the event loop.
        entry_dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "usd": usd,
            "stream": stream,
            "reason": reason,
        }
        if cached_tokens:
            entry_dict["cached_tokens"] = cached_tokens
        if cache_savings_usd:
            entry_dict["cache_savings_usd"] = cache_savings_usd
        entry = json.dumps(entry_dict)

        def _write_ledger() -> None:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")

        try:
            await asyncio.to_thread(_write_ledger)
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
                            m: {"usd": round(mv["usd"], 8), "requests": mv["requests"],
                                "cached_tokens": mv.get("cached_tokens", 0),
                                "est_cache_savings_usd": round(
                                    mv.get("est_cache_savings_usd", 0.0), 8)}
                            for m, mv in sorted(pv["by_model"].items())
                        },
                    }
                    for provider, pv in sorted(self._by_provider.items())
                },
            }


# --- A2: adequacy ledger (observe-only) ----------------------------------------
# One metadata-only entry per /v1/chat/completions request (spec 2b). Never
# message content (risk 7). `shadow` ships now, always False, so the v0.3
# shadow evaluator appends to the same schema instead of migrating it.

from dataclasses import dataclass, field  # noqa: E402


@dataclass
class Observation:
    """Filled across a request's lifetime: routing fields at dispatch,
    outcome fields when the response completes."""
    cls: str
    classifier_version: int
    requested_model: str
    route: str                      # "local" | "cloud"
    served_model: str
    reason: str
    stream: bool
    status: int | None = None
    latency_ms: int | None = None
    ttfb_ms: int | None = None
    fallback_fired: bool = False
    finish_reason: str | None = None
    had_tool_calls: bool = False
    tool_calls_valid_json: bool | None = None
    usage: dict[str, Any] | None = field(default=None, repr=False)
    usd: float = 0.0
    session_id: str | None = None
    fallback_at_ms: int | None = None  # tracing-only: elapsed-ms when local->cloud retry began; not serialized to the ledger

    def to_entry(self) -> dict[str, Any]:
        u = self.usage or {}
        ptd = u.get("prompt_tokens_details") or {}
        ctd = u.get("completion_tokens_details") or {}
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "class": self.cls,
            "classifier_version": self.classifier_version,
            "requested_model": self.requested_model,
            "route": self.route,
            "served_model": self.served_model,
            "reason": self.reason,
            "stream": self.stream,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "ttfb_ms": self.ttfb_ms,
            "fallback_fired": self.fallback_fired,
            "finish_reason": self.finish_reason,
            "had_tool_calls": self.had_tool_calls,
            "tool_calls_valid_json": self.tool_calls_valid_json,
            "tokens": {
                "prompt": u.get("prompt_tokens"),
                "completion": u.get("completion_tokens"),
                "reasoning": ctd.get("reasoning_tokens"),
                "cached": ptd.get("cached_tokens"),
            },
            "usd": self.usd,
            "session_id": self.session_id,
            "shadow": False,
        }


class StreamScan:
    """Accumulates adequacy signals from teed SSE lines (or a non-stream body).

    Signals are computed from the response body, never trusted fields
    (spec risk 6). Any malformed line is ignored — scanning must never
    affect the proxied response.
    """

    def __init__(self) -> None:
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.had_tool_calls = False
        self._tool_args: dict[int, list[str]] = {}

    def feed_line(self, line: bytes) -> None:
        try:
            text = line.decode("utf-8", errors="replace").strip()
            if not text.startswith("data:"):
                return
            payload = text[5:].strip()
            if payload == "[DONE]":
                return
            self._feed_obj(json.loads(payload), streaming=True)
        except Exception:  # noqa: BLE001 - the tee must never break the stream
            pass

    @classmethod
    def from_response_body(cls, obj: dict[str, Any]) -> "StreamScan":
        scan = cls()
        try:
            scan._feed_obj(obj, streaming=False)
        except Exception:  # noqa: BLE001
            pass
        return scan

    def _feed_obj(self, obj: dict[str, Any], streaming: bool) -> None:
        u = obj.get("usage")
        if isinstance(u, dict) and u:
            self.usage = u
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            fr = ch.get("finish_reason")
            if fr:
                self.finish_reason = fr
            container = ch.get("delta") if streaming else ch.get("message")
            for i, tc in enumerate((container or {}).get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                self.had_tool_calls = True
                idx = tc.get("index", i)
                args = (tc.get("function") or {}).get("arguments")
                if args:
                    self._tool_args.setdefault(idx, []).append(args)

    def tool_calls_valid(self) -> bool | None:
        if not self.had_tool_calls:
            return None
        try:
            for parts in self._tool_args.values():
                json.loads("".join(parts))
            return True
        except Exception:  # noqa: BLE001
            return False


def resolve_adequacy_ledger() -> pathlib.Path | None:
    """$ADEQUACY_LEDGER override ("" disables) > state_dir()/adequacy.jsonl.
    No legacy fallback — this file has never lived anywhere else."""
    env = os.environ.get("ADEQUACY_LEDGER")
    if env is not None:
        return pathlib.Path(env) if env else None
    return state_dir() / "adequacy.jsonl"


class AdequacyLedger:
    """Append-only JSONL writer for Observation entries. Same discipline as
    SpendTracker: threaded IO off the event loop, failures logged not raised."""

    def __init__(self, ledger_path: pathlib.Path | None,
                 log: Callable[[str], None] = lambda _m: None):
        self.ledger_path = ledger_path
        self._log = log

    async def write(self, obs: "Observation") -> None:
        if not self.ledger_path:
            return
        entry = json.dumps(obs.to_entry())

        def _append() -> None:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")

        try:
            await asyncio.to_thread(_append)
        except Exception as e:  # noqa: BLE001 - never break a response over the ledger
            self._log(f"[router] adequacy ledger write failed ({e}); entry dropped")
