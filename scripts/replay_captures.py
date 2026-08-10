#!/usr/bin/env python3
"""Replay captured loxo requests through both tiers, for the absorption Number.

Usage:
  python3 scripts/replay_captures.py [CAPTURE_DIR] [--out results.jsonl] [options]

For each req-<ts>-<seq>.json capture, re-issues the request to the running
loxo router TWICE -- once forced LOCAL (loxo/local) and once forced CLOUD
(loxo/deep) -- and records both responses, token usage, latency, and the
backend model each tier resolved to. One JSONL row per capture.

This is the raw local-vs-frontier comparison the local-absorption Number is
computed from. It deliberately does NOT do two things, which are separate
passes:
  - It does not JUDGE adequacy. That is mechanical for `compaction` and
    hand-judged for `main` (see the plan). This script only produces the
    two outputs to compare.
  - It does not compute cost. It records `usage` (prompt/completion tokens);
    dollars are derived downstream from the rate card in loxo's /health.

Each capture is also tagged with the router's OWN class via classify(), so
the results are sliceable per class (main / chore / compaction / unknown)
straight out of the file.

Deliberately SEQUENTIAL -- one in-flight request at a time -- so a big local
model can't stack KV caches and OOM the machine (learned the hard way on a
24 GB Mac). Resumable: captures already present in --out are skipped, so a
crash mid-run just re-runs the remainder.

Faithful replay except three overrides, each to make the comparison clean:
  - model       -> loxo/local, then loxo/deep   (the whole point)
  - stream      -> False                         (so one JSON carries usage)
  - temperature -> --temperature (default 0.0, for reproducibility; pass
                  --keep-temperature to leave the captured value untouched)

Requires loxo importable (for classify): run inside the loxo venv, or
`pip install -e .` in loxo-llm-router first. loxo must be RUNNING and
reachable at --loxo-url (both local and cloud backends configured).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

try:
    from loxo_llm_router.classify import classify
except ImportError:
    sys.exit("loxo_llm_router not importable -- run inside the loxo venv (pip install -e .).")

DEFAULT_CAPTURES = pathlib.Path.home() / ".local/state/loxo-llm-router/captures"
DEFAULT_LOXO_URL = "http://localhost:9090/v1/chat/completions"
LOCAL_TIER = "loxo/local"
DEEP_TIER = "loxo/deep"


def _post(url: str, body: dict, timeout: float):
    """POST a chat-completion body; return (elapsed_s, {ok, response|error})."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        return time.monotonic() - t0, {"ok": True, "response": payload}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        return time.monotonic() - t0, {"ok": False, "error": f"HTTP {e.code}", "detail": detail}
    except Exception as e:  # noqa: BLE001 - timeout, conn refused, bad JSON: record, don't abort
        return time.monotonic() - t0, {"ok": False, "error": type(e).__name__, "detail": str(e)[:2000]}


def _extract(result: dict) -> dict:
    """Reduce a raw loxo response to the fields the Number needs (errors pass through)."""
    if not result.get("ok"):
        return result
    r = result["response"]
    choice = (r.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {
        "ok": True,
        "backend_model": r.get("model"),        # the model loxo actually dispatched to
        "content": msg.get("content"),
        "tool_calls": msg.get("tool_calls"),
        "finish_reason": choice.get("finish_reason"),
        "usage": r.get("usage"),                # prompt/completion tokens -> cost downstream
    }


def _replay_one(body: dict, url: str, temperature: float, keep_temp: bool, timeout: float) -> dict:
    """Run one capture through both tiers; return {"local": rec, "deep": rec}."""
    out = {}
    for tier_key, tier_model in (("local", LOCAL_TIER), ("deep", DEEP_TIER)):
        sent = dict(body)
        sent["model"] = tier_model
        sent["stream"] = False
        if not keep_temp:
            sent["temperature"] = temperature
        elapsed, result = _post(url, sent, timeout)
        rec = _extract(result)
        rec["latency_s"] = round(elapsed, 2)
        out[tier_key] = rec
    return out


def _already_done(out_path: pathlib.Path) -> set:
    """Capture filenames already recorded in --out, for resume."""
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                done.add(json.loads(line)["capture"])
            except Exception:  # noqa: BLE001 - a half-written final line shouldn't break resume
                pass
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay loxo captures through local + deep tiers.")
    ap.add_argument("captures_dir", nargs="?", default=str(DEFAULT_CAPTURES),
                    help=f"dir of req-*.json captures (default: {DEFAULT_CAPTURES})")
    ap.add_argument("--out", default="replay-results.jsonl",
                    help="output JSONL, appended (default: replay-results.jsonl)")
    ap.add_argument("--loxo-url", default=DEFAULT_LOXO_URL,
                    help=f"loxo chat endpoint (default: {DEFAULT_LOXO_URL})")
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="override temperature for both tiers (default: 0.0)")
    ap.add_argument("--keep-temperature", action="store_true",
                    help="leave the captured temperature untouched instead of forcing --temperature")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="per-request timeout seconds (default: 600)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only replay the first N pending captures (0 = all; use for a smoke test)")
    args = ap.parse_args()

    captures = sorted(pathlib.Path(args.captures_dir).glob("req-*.json"))
    if not captures:
        sys.exit(f"no req-*.json found in {args.captures_dir}")

    out_path = pathlib.Path(args.out)
    done = _already_done(out_path)
    if done:
        print(f"resuming: {len(done)} already in {out_path}, skipping those", file=sys.stderr)

    todo = [f for f in captures if f.name not in done]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("nothing to do (all captures already replayed)", file=sys.stderr)
        return

    with out_path.open("a") as out:
        for i, f in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {f.name} ...", file=sys.stderr, flush=True)
            try:
                body = json.loads(f.read_text())
                if not isinstance(body, dict):
                    raise ValueError(f"capture root is {type(body).__name__}, not an object")
                cls = classify(body).cls
                tiers = _replay_one(body, args.loxo_url, args.temperature,
                                    args.keep_temperature, args.timeout)
                row = {"capture": f.name, "cls": cls, **tiers}
            except Exception as e:  # noqa: BLE001 - one bad capture must not end the batch
                row = {"capture": f.name, "error": type(e).__name__, "detail": str(e)[:2000]}
            out.write(json.dumps(row) + "\n")
            out.flush()

    print(f"done -- results in {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
