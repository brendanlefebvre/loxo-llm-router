# Spike: local-absorption pilot — the first Number (2026-08)

**Question** (ROADMAP win condition 1): of the token volume in real captured
sessions, what fraction could the local tier actually serve? This pilot
measures the *mechanical availability ceiling* — did local emit an answer at
all — not yet adequacy-proven absorption.

**Method:** `scripts/replay_captures.py` over a frozen 96-capture corpus,
each capture replayed through the forced local tier (`loxo/local` →
`mlx-community/Qwen3-14B-4bit`, 40,960-token context) and the forced deep tier
(`loxo/deep` → `google/gemini-2.5-pro`). One JSONL row per capture with both
tiers' `usage`, `finish_reason`, and error surface. Hardware: 24 GB Apple
Silicon Mac; MLX served via `mlx-lm.server` (port 7979).

**Corpus hygiene (this is load-bearing):** the capture dir had been polluted by
an earlier replay run feeding on its own output — a capture-enabled router
captures the replay's own requests, doubling the pool with
`stream:false`/`temperature:0.0` artifacts every pass (301 files, only 96
organic). The 96 organic captures (84 `loxo/deep` + 8 `loxo/auto` +
4 `loxo/balanced`, all `stream:true`, none carrying `temperature`) were
separated from the 205 artifacts by that fingerprint and frozen. The self-feed
loop is now closed at the source: the harness sends `X-Loxo-Replay: 1` and
`_capture_request` skips it (see `test_capture.py`). Class mix of the 96:
93 `main`, 2 `chore`, 1 `compaction`.

**Integrity checks:** every local success ran on Qwen3-14B (no cloud model
masquerading as local via fallback); deep succeeded on all 96, always
gemini-2.5-pro.

## The Number

**Local-absorption ceiling ≈ 15.1% of prompt-token volume** (573,584 /
3,808,368 prompt tokens; 27 / 96 requests = 28% by count). It is lower by token
than by count because the requests local *can* take are the small ones.

The arc: the first pass read 9.4% because 12 captures failed on MLX server
errors that looked transient. Re-running them (with the MLX server confirmed up
and watched) resolved 8 to `served` and exposed the remaining failures as a
*real* capacity wall — not noise — landing the honest figure at 15.1%. The
30k–31k band is genuinely stochastic (see the OOM finding), so treat 15.1% as a
point estimate with real noise, not a sharp line.

### Local outcome taxonomy (96 captures, final)

| outcome | count | meaning |
|---|---:|---|
| served | 27 | local produced an answer |
| overflow | 65 | loxo's own 422 "prompt too large for local context (> 40960)" |
| OOM (HTTP 500) | 2 | MLX crashed on a Metal GPU out-of-memory during prefill |
| unreachable (HTTP 422) | 2 | request landed while MLX was reloading after a prior OOM crash |

## Finding 1: main is a context wall (structural, dominant)

65 of 93 `main` captures overflow the 40,960-token local context and get a
clean 422 from loxo's own guard. **~90% of the main-turn token volume is beyond
a 40k-context 14B.** This is the dominant fact: on this hardware, most main
traffic is simply too big for the local model, guard or no guard.

(Prompt sizes in this doc are gemini-tokenizer counts from the deep tier;
loxo's own local-context estimate runs higher, which is why the overflow cutoff
appears around 31k gemini-tokens rather than exactly 40,960.)

## Finding 2: the near-ceiling failures are a GPU-memory OOM, path-dependent on the cache

The 12 "infra" failures were **not** transient noise. Re-running them under a
watched MLX server produced the real story:

- **It is a Metal GPU out-of-memory**, not a process-RSS blowup:
  `libc++abi: terminating … [METAL] Command buffer execution failed:
  Insufficient Memory (kIOGPUCommandBufferCallbackErrorOutOfMemory)`. The crash
  happens *during prefill* (KV-cache allocation), and MLX then restarts. Process
  RSS stayed flat at ~8.3 GB throughout — the KV cache lives in Metal GPU
  buffers `ps` does not attribute, and the real cap is the **GPU wired-memory
  limit** (`iogpu.wired_limit_mb`, ~16 GB by default on a 24 GB machine).

- **It is path-dependent, not size-deterministic.** The smoking gun: the
  *largest* main prompt that SERVED was **31,230 tokens**, while the *smallest*
  one that FAILED was **30,452**. A bigger prompt succeeded while a smaller one
  OOM'd — impossible under a size cutoff. What decides the outcome is the GPU
  memory *state* when the prompt lands. A successful near-ceiling request builds
  a large prompt cache (observed: one built a **5.24 GB** cache); the next
  request then hits `model (~8 GB) + residual cache (~5.2 GB)` already occupying
  ~13 of the ~16 GB wired budget and OOMs almost immediately. The clincher: a
  **631-token** request was seen OOMing — a prompt that tiny cannot OOM on its
  own; it only fails because the cache had already consumed the wired budget.

- **The failure cascades into two error codes.** One oversized prompt OOMs →
  MLX crashes and reloads → whatever request lands *during the reload window*
  logs "unreachable". That is why the original 12 split into 500s (the OOM
  trigger) and 422 "unreachable" (collateral during reload). Same phenomenon.

**Net:** local's *effective* usable context under the default wired limit is
~30k tokens for a single request on a clean server, and **less under
back-to-back load** because the prompt cache accumulates and eats the headroom.
The nominal 40,960 is never the operative limit here.

## The loxo fix (actionable, two-part)

1. **Lower `LOCAL_CONTEXT_LIMIT`** from 40,960 to the wired-limit-determined real
   ceiling (~30k at the ~16 GB default). Today loxo waves prompts through that
   crash the MLX server (500 + a full model-reload thrash) instead of rejecting
   them cleanly (422 → route to cloud). Re-measure and re-set if the wired limit
   is ever changed.
2. **Cap or evict MLX's prompt cache** (mlx-lm's max-kv / cache-size knob). The
   context limit bounds any *single* prompt, but a small prompt can still OOM
   when a fat cached sequence has already consumed the wired budget — so the
   cache cap is what keeps the *next* request alive. For a shared local tier,
   trading cache-reuse speed for reliable headroom is the right call.

(Optional lever, not recommended as default: `sudo sysctl
iogpu.wired_limit_mb=21504` raises the GPU budget and pushes the ceiling up, but
on 24 GB it starves the OS + OpenCode + loxo — fragile for an always-on tier.)

## Per class (served / total, prompt-token ceiling)

| class | served/total | token ceiling |
|---|---|---|
| chore | 2/2 | 100.0% |
| compaction | 0/1 | 0.0% — **still unmeasured** (see below) |
| main | 25/93 | 15.1% |

**Compaction remains a blank.** The single compaction capture (35,183 tokens)
failed on both re-runs — but each time as reload-race "unreachable" collateral
from a *preceding* main OOM, never on its own clean attempt. To get a real
compaction data point it must be run in isolation on a freshly-restarted MLX
server. At 35k it may OOM anyway (above the overlap band), but it deserves one
clean shot before the cell is called.

## The governing caveat: availability ≠ proven adequacy

Everything above is *mechanical availability*. None of the successes are
quality-judged against the deep tier, and per ROADMAP the main-turn quality
judge is an explicitly deferred open question. So absorption-**with-proven-
adequacy** (the actual win condition 1) is computable today only for the
`chore` (+ eventual `compaction`) slice; the `main` share of the 15.1% is
availability, not yet earned.

## Next actions

1. Ship the loxo fix above (LOCAL_CONTEXT_LIMIT + prompt-cache cap).
2. One isolated run of the 35k compaction capture on a clean MLX server, to
   fill the compaction cell.
3. Stand up the adequacy pass: mechanical for `compaction`, hand-judged for
   `main`, to convert "served" into "adequately served."
4. Grow the organic corpus past 96 (real sessions with capture on) — 93/2/1 is
   thin for the chore and compaction classes where local actually wins.

## Commands

```
# frozen corpus (organic only), self-feed guard live in the router
python3 scripts/replay_captures.py ~/loxo-corpus-frozen --out replay-results.jsonl

# re-run a subset: strip their rows so the harness re-issues them, with the
# MLX server confirmed up (put the filenames to re-run in retry.txt)
grep -vFf retry.txt replay-results.jsonl > tmp && mv tmp replay-results.jsonl
python3 scripts/replay_captures.py ~/loxo-corpus-frozen --out replay-results.jsonl
```
