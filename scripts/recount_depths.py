#!/usr/bin/env python3
"""Report the depth distribution of captured requests against Rule 3's threshold.

Answers "how much of my traffic is even eligible for local routing" by bucketing
LOXO_CAPTURE_DIR into shallow / mid / in-scope-deep / over-limit.

**The estimator and the limit are imported, never reimplemented.** Rule 3 fires
on `estimate_prompt_tokens(body) > LOCAL_CONTEXT_LIMIT`, so any answer about
which side of that line a request falls on has to use those exact two things.
A hand-rolled char-count proxy was tried on 2026-07-27 and inverted the
conclusion — it reported 63% of traffic over the limit where the real figure was
12% — because it counted the serialized message array (picking up role keys and
tool_call blobs) while omitting the tools array entirely. Two errors in opposite
directions, both invisible.

There is deliberately no fallback: inside this repo the import cannot fail for
an interesting reason, and a copy of `estimate_prompt_tokens` living here would
be the very drift hazard this script exists to demonstrate.

Prints no message content.
"""
import json
import os
import pathlib
import sys

from loxo_llm_router import LOCAL_CONTEXT_LIMIT as limit
from loxo_llm_router import estimate_prompt_tokens as est

CAPS = pathlib.Path(os.environ.get("LOXO_CAPTURE_DIR")
                    or os.path.expanduser("~/.local/state/loxo-llm-router/captures"))

print("estimator: imported from loxo_llm_router (authoritative)")
print(f"LOCAL_CONTEXT_LIMIT: {limit}")
print(f"captures: {CAPS}\n")

vals, over, tools_only = [], 0, []
for f in sorted(CAPS.glob("*.json")):
    try:
        body = json.loads(f.read_text(encoding="utf-8"))
    except Exception:                                                   # noqa: BLE001
        continue
    n = est(body)
    vals.append(n)
    if n > limit:
        over += 1
    # how much of the estimate is tool schemas alone -- constant per harness version
    t = sum(len(json.dumps(body[k])) for k in ("tools", "functions") if k in body) // 4
    tools_only.append(t)

if not vals:
    print("no captures found"); sys.exit(1)

vals.sort()
def pct(p):
    return vals[min(len(vals) - 1, int(len(vals) * p))]

shallow = sum(1 for v in vals if v < 16000)
mid = sum(1 for v in vals if 16000 <= v < 40000)
deep_in = sum(1 for v in vals if 40000 <= v <= limit)
deep_over = sum(1 for v in vals if v > limit)

print(f"{'stratum':<28} {'n':>5}   {'share':>6}")
print("-" * 44)
for label, n in (("shallow  (<16k)", shallow),
                 ("mid      (16k-40k)", mid),
                 (f"deep     (40k-{limit//1000}k, IN SCOPE)", deep_in),
                 (f"OVER LIMIT (>{limit//1000}k, Rule 3 -> cloud)", deep_over)):
    print(f"{label:<28} {n:>5}   {n/len(vals)*100:>5.1f}%")

in_scope = shallow + mid + deep_in
print("-" * 44)
print(f"{'IN-SCOPE TOTAL':<28} {in_scope:>5}   {in_scope/len(vals)*100:>5.1f}%")
print(f"\nmin {vals[0]}  p50 {pct(0.50)}  p90 {pct(0.90)}  max {vals[-1]}")
print(f"tool schemas alone: ~{sum(tools_only)//len(tools_only)} tokens/request "
      f"(fixed floor every request pays)")


