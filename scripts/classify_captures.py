#!/usr/bin/env python3
"""Classify loxo capture dumps and compare across dirs (e.g. v1 vs v2 sidecar).

Usage:
  python3 classify_captures.py /tmp/cap-v1 /tmp/cap-v2

Requires loxo importable: run inside the loxo venv, or `pip install -e .`
in loxo-llm-router first. Reads the raw request bodies loxo writes when
LOXO_CAPTURE_DIR is set (req-<ts>-<seq>.json) and runs each through the
same classify() the router uses, so the verdict is the router's, not an
eyeball diff.
"""
import sys
import json
import pathlib
from collections import Counter

try:
    from loxo_llm_router.classify import classify
except ImportError:
    sys.exit("loxo_llm_router not importable — run inside the loxo venv (pip install -e .).")


def system_opener(body, n=90):
    """First system/developer message text — the substring classify() fingerprints on.

    Mirrors classify.py's _system_text(): list content joins ALL text parts.
    Returning only the first would print an opener that omits the fingerprint
    the verdict was actually reached on, which is the one thing this column
    exists to show.
    """
    for m in body.get("messages", []):
        if not (isinstance(m, dict) and m.get("role") in ("system", "developer")):
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c[:n]
        if isinstance(c, list):
            return " ".join(
                part.get("text", "") for part in c
                if isinstance(part, dict) and part.get("type") == "text"
            )[:n]
    return "(no system/developer message)"


def scan(d):
    rows = []
    for f in sorted(pathlib.Path(d).glob("req-*.json")):
        try:
            body = json.loads(f.read_text())
            if not isinstance(body, dict):
                # A JSON array or scalar parses fine but has no .get(), so it
                # would raise inside classify() and kill the batch this handler
                # exists to keep alive.
                raise ValueError(f"capture root is {type(body).__name__}, not an object")
        except Exception as e:  # noqa: BLE001 - report, don't abort the batch
            rows.append((f.name, "PARSE-ERR", str(e)[:60]))
            continue
        rows.append((f.name, classify(body).cls, system_opener(body)))
    return rows


def main(dirs):
    missing = [d for d in dirs if not pathlib.Path(d).is_dir()]
    if missing:
        sys.exit("not a capture directory: " + ", ".join(missing))
    for d in dirs:
        rows = scan(d)
        counts = Counter(r[1] for r in rows)
        print(f"\n=== {d}  ({len(rows)} requests) ===")
        for name, cls, opener in rows:
            flag = "  <-- UNKNOWN (fingerprint miss)" if cls == "unknown" else ""
            print(f"  {cls:11} {name}{flag}")
            print(f"              opener: {opener!r}")
        print(f"  totals: {dict(counts)}")
        classified = len(rows) - counts.get("PARSE-ERR", 0)
        if classified:
            print(f"  unknown rate: {counts.get('unknown', 0) / classified:.0%}"
                  f"  ({classified} classified)")
        else:
            print("  unknown rate: n/a (nothing classified)")
    print(
        "\nVerdict: if the v2 dir's main-turn request classifies 'main', the "
        "fingerprints held.\nIf it flipped to 'unknown', the sidecar moved the "
        "prompt — re-capture, sanitize into a fixture, extend MAIN_FINGERPRINTS, "
        "bump CLASSIFIER_VERSION."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: classify_captures.py DIR [DIR2 ...]")
    main(sys.argv[1:])
