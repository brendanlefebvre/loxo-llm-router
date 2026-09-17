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
24 GB Mac). Resumable: captures with a terminal row in --out (a server verdict,
success or HTTP error) are skipped; transport-error rows (server down, timeout)
are dropped and those captures retried, so a crash or outage mid-run just
re-runs the remainder.

If the router has ROUTER_TOKEN set, pass --token (or export ROUTER_TOKEN);
otherwise every replay 401s.

Faithful replay except three overrides, each to make the comparison clean:
  - model       -> loxo/local, then loxo/deep   (the whole point)
  - stream      -> False                         (so one JSON carries usage)
  - temperature -> --temperature (default 0.0, for reproducibility; pass
                  --keep-temperature to leave the captured value untouched)

Requires loxo importable (for classify): run inside the loxo venv, or
`pip install -e .` in loxo-llm-router first. loxo must be RUNNING and
reachable at --loxo-url (both local and cloud backends configured).

Every replay request carries the X-Loxo-Replay header, which tells a
capture-enabled router to skip capturing it -- without this a replay run
against a live-capturing server feeds on its own output, doubling the
capture corpus with stream:false/temperature:0.0 artifacts every pass.
"""
from __future__ import annotations

import argparse
import json
import os
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


def _build_request(url: str, body: dict, token: str = "") -> urllib.request.Request:
    """Build the replay POST: JSON body, replay marker, optional bearer token."""
    headers = {
        "Content-Type": "application/json",
        # X-Loxo-Replay tells a capture-enabled router NOT to capture this
        # request -- otherwise the replay feeds on its own output.
        "X-Loxo-Replay": "1",
    }
    if token:
        # Without this, a ROUTER_TOKEN-enabled router 401s every capture in
        # seconds and the run looks "complete" with zero data.
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST",
    )


def _post(url: str, body: dict, timeout: float, token: str = ""):
    """POST a chat-completion body; return (elapsed_s, {ok, response|error})."""
    req = _build_request(url, body, token)
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


def _replay_one(body: dict, url: str, temperature: float, keep_temp: bool,
                timeout: float, token: str = "") -> dict:
    """Run one capture through both tiers; return {"local": rec, "deep": rec}."""
    out = {}
    for tier_key, tier_model in (("local", LOCAL_TIER), ("deep", DEEP_TIER)):
        sent = dict(body)
        sent["model"] = tier_model
        sent["stream"] = False
        # stream:false + stream_options is OpenAI-spec-invalid; a strict
        # backend's 400 would be misread as a tier failure.
        sent.pop("stream_options", None)
        if not keep_temp:
            sent["temperature"] = temperature
        elapsed, result = _post(url, sent, timeout, token)
        rec = _extract(result)
        rec["latency_s"] = round(elapsed, 2)
        out[tier_key] = rec
    return out


def _tier_terminal(rec) -> bool:
    """A tier record is terminal when the server rendered a verdict: success,
    or an HTTP error (422 overflow, 500 OOM). A transport error (connection
    refused, timeout -- recorded as the exception's type name) means the server
    never judged the request, so the capture must be retried on resume."""
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("ok")) or str(rec.get("error", "")).startswith("HTTP ")


def _row_terminal(row: dict) -> bool:
    if "error" in row:  # row-level error: deterministic capture-parse failure
        return True
    return _tier_terminal(row.get("local")) and _tier_terminal(row.get("deep"))


def _compact_for_resume(out_path: pathlib.Path) -> set:
    """Return capture filenames with a terminal row in --out; rewrite the file
    keeping only those rows so retried captures never appear twice.

    Counting EVERY row as done made resume useless after an outage: a run
    against a down router appended 96 transport-error rows in seconds, and the
    rerun said "nothing to do" -- recovery used to be manual grep surgery.
    Half-written final lines (crash mid-write) are dropped and retried too."""
    if not out_path.exists():
        return set()
    kept, done = [], set()
    for line in out_path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001 - half-written final line: retry that capture
            continue
        if _row_terminal(row):
            kept.append(line)
            done.add(row["capture"])
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text("".join(line + "\n" for line in kept))
    tmp.replace(out_path)
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
    ap.add_argument("--token", default=os.environ.get("ROUTER_TOKEN", ""),
                    help="router bearer token (default: $ROUTER_TOKEN; required "
                         "when the router has ROUTER_TOKEN set)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only replay the first N pending captures (0 = all; use for a smoke test)")
    args = ap.parse_args()

    captures = sorted(pathlib.Path(args.captures_dir).glob("req-*.json"))
    if not captures:
        sys.exit(f"no req-*.json found in {args.captures_dir}")

    out_path = pathlib.Path(args.out)
    done = _compact_for_resume(out_path)
    if done:
        print(f"resuming: {len(done)} terminal in {out_path}, skipping those "
              "(transport-error rows dropped for retry)", file=sys.stderr)

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
                                    args.keep_temperature, args.timeout, args.token)
                row = {"capture": f.name, "cls": cls, **tiers}
            except Exception as e:  # noqa: BLE001 - one bad capture must not end the batch
                row = {"capture": f.name, "error": type(e).__name__, "detail": str(e)[:2000]}
            out.write(json.dumps(row) + "\n")
            out.flush()

    print(f"done -- results in {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
