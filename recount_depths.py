#!/usr/bin/env python3
"""Recount capture depths using Loxo's OWN estimator and OWN configured limit.

The point is fidelity to the routing decision: Rule 3 fires on
estimate_prompt_tokens() > LOCAL_CONTEXT_LIMIT, so those are the two things
that must come from the package rather than a replica.

Run from anywhere the loxo_llm_router package is importable (e.g. the repo root
with its venv active). Falls back to an inline copy of the function if the
import has side effects you'd rather avoid -- but prefer the import.

Prints no message content.
"""
import json
import os
import pathlib
import sys

CAPS = pathlib.Path(os.environ.get("LOXO_CAPTURE_DIR")
                    or os.path.expanduser("~/.local/state/loxo-llm-router/captures"))

est = None
limit = None
try:
    from loxo_llm_router import estimate_prompt_tokens as est          # noqa: E402
    from loxo_llm_router import LOCAL_CONTEXT_LIMIT as limit           # noqa: E402
    src = "imported from loxo_llm_router (authoritative)"
except Exception as e:                                                  # noqa: BLE001
    src = f"FALLBACK inline copy -- import failed: {e}"

    def est(body):                                                      # type: ignore[misc]
        total = 0
        for m in body.get("messages", []):
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += len(part.get("text", ""))
        for key in ("tools", "functions"):
            if key in body:
                total += len(json.dumps(body[key]))
        return total // 4

    limit = int(os.environ.get("LOCAL_CONTEXT_LIMIT") or 60000)

print(f"estimator: {src}")
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


