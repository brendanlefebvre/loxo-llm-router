#!/usr/bin/env python3
"""Group captured requests into prefix chains, and report the usable pairs.

Successive captures from one client session are not independent: each one's
`messages` array is a byte-identical prefix of the next, because the session
grows by appending. That means capture N ends where a model had to act and
capture N+1 contains what it actually did — so a chain of length K yields K-1
(state, next-action) pairs of real production behaviour.

This walks LOXO_CAPTURE_DIR in filename order (timestamped, so chronological)
and for each consecutive pair reports whether the prefix relationship holds,
diverges (a different conversation), or shortens (a compaction rewrites history
rather than appending — the seam any consumer must detect rather than assume).

Downstream consumers should stratify by chain as well as by depth: pairs drawn
from a single chain share one task, so "deeper context" and "later in this task"
cannot be told apart within one chain.

A pair is only a decision point if the appended messages begin with an
`assistant` message; the roles column shows this.

Prints NO message content -- only counts, roles, and truncated hashes.

Usage:
    python3 scripts/check_prefix_chain.py [capture_dir]
"""
import hashlib
import json
import os
import pathlib
import sys

d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                 else os.path.expanduser("~/.local/state/loxo-llm-router/captures"))

def mhash(m):
    """Stable hash of one message, order-insensitive to key order."""
    return hashlib.sha1(json.dumps(m, sort_keys=True,
                                   ensure_ascii=False).encode("utf-8")).hexdigest()[:10]

rows = []
for f in sorted(d.glob("*.json")):
    try:
        body = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"SKIP {f.name}: {e}")
        continue
    msgs = body.get("messages") or []
    rows.append({
        "name": f.name,
        "n": len(msgs),
        "tools": len(body.get("tools") or []),
        "h": [mhash(m) for m in msgs],
        "roles": [m.get("role") for m in msgs],
    })

print(f"{len(rows)} captures in {d}\n")
print(f"{'capture':44} {'n':>4} {'tools':>5}  relationship to previous")
print("-" * 108)

chains, cur = [], []
prev = None
for r in rows:
    if prev is None:
        rel = "CHAIN START"
        cur = [r["name"]]
    else:
        common = min(prev["n"], r["n"])
        # first index where the two message arrays disagree
        div = next((i for i in range(common) if prev["h"][i] != r["h"][i]), None)
        if div is not None:
            rel = f"** DIVERGES at msg {div} (was {prev['h'][div]}, now {r['h'][div]}) -> NEW CHAIN"
            chains.append(cur)
            cur = [r["name"]]
        elif r["n"] < prev["n"]:
            rel = f"** SHORTER ({prev['n']} -> {r['n']}) -- compaction/reset -> NEW CHAIN"
            chains.append(cur)
            cur = [r["name"]]
        elif r["n"] == prev["n"]:
            rel = "identical prefix, no growth (retry/duplicate?)"
            cur.append(r["name"])
        else:
            added = r["roles"][prev["n"]:]
            rel = f"prefix OK, +{r['n'] - prev['n']} msg [{','.join(str(x) for x in added)}]"
            cur.append(r["name"])
    print(f"{r['name']:44} {r['n']:>4} {r['tools']:>5}  {rel}")
    prev = r
chains.append(cur)

print("\n" + "=" * 108)
usable = 0
for i, c in enumerate(chains, 1):
    print(f"chain {i}: {len(c):>4} captures  ({c[0]} .. {c[-1]})")
    usable += max(0, len(c) - 1)
print(f"\n{len(chains)} chain(s); {usable} consecutive (state, next-action) pairs available.")
print("A pair is usable only if the appended role list starts with 'assistant'.")

