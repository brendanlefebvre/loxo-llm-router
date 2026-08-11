# Spike: local-absorption pilot — the first Number (2026-08)

**Question** (ROADMAP win condition 1): of the token volume in real captured
sessions, what fraction could the local tier actually serve? This pilot
measures the *mechanical availability ceiling* — did local emit an answer at
all — not yet adequacy-proven absorption.

**Method:** `scripts/replay_captures.py` over a frozen 96-capture corpus,
each capture replayed through the forced local tier (`loxo/local` →
`mlx-community/Qwen3-14B-4bit`, 40,960-token context) and the forced deep tier
(`loxo/deep` → `google/gemini-2.5-pro`). One JSONL row per capture with both
tiers' `usage`, `finish_reason`, and error surface.

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

**Integrity checks:** local served on Qwen3-14B for all 19 successes (no cloud
model masquerading as local via fallback); deep succeeded on all 96, always
gemini-2.5-pro.

## The Number

**Local-absorption ceiling ≈ 9.4% of prompt-token volume, as-shipped**
(359,370 / 3,808,368 prompt tokens; 19 / 96 requests = 19.8% by count). It is
lower by token than by count because the requests local *can* take are the
small ones.

**The honest figure is a range, 9.4%–18%**, because 12 captures failed on
transient MLX errors rather than a capacity verdict (see Noise below). Collapse
the range by re-running those 12.

### Local outcome taxonomy (96 captures)

| outcome | count | meaning |
|---|---:|---|
| served | 19 | local produced an answer |
| overflow | 65 | loxo's own 422 "prompt too large for local context (> 40960)" |
| infra | 12 | 9× HTTP 500, 3× HTTP 422 "local server unreachable" |

The overflow and infra buckets are cleanly distinguishable: loxo emits a
*specific* 422 "prompt too large" when its context guard trips, so the 500s and
the "server unreachable" 422s are server-health failures, not capacity verdicts.

## Signal: main is a context wall (structural, not fixable cheaply)

65 of 93 `main` captures overflow the 40,960-token local ceiling. **~90% of the
main-turn token volume is beyond a 40k-context 14B.** Widening local context to
recover it means a larger KV cache — straight into the 24 GB OOM the replay
harness itself warns about. This is the real finding: on this hardware, the
main class is not locally absorbable at the model/context in play.

(Prompt sizes in this doc are gemini-tokenizer counts from the deep tier;
loxo's own local-context estimate runs higher, which is why the effective
overflow cutoff appears around 31k gemini-tokens rather than exactly 40,960.)

## Noise: 12 infra failures currently understating the ceiling

All 12 infra-failed captures (14k–35k prompt tokens, incl. the *only*
`compaction` capture at 35,183) failed on MLX server errors, not overflow — the
errors cluster in a tight 2026-08-10 evening timestamp window, i.e. the MLX
server was flaky, not the prompts too big. They are currently scored as local
losses, which drags the Number down. Re-running them (MLX confirmed up):

- `chore` stays 100% (2/2),
- `compaction` almost certainly flips 0 → 100% (its 35k prompt fits; it only
  500'd),
- the token ceiling moves **9.4% → up to ~18%** (the 12 carry ~342k prompt
  tokens).

## Per class (served / total, prompt-token ceiling)

| class | served/total | token ceiling |
|---|---|---|
| chore | 2/2 | 100.0% |
| compaction | 0/1 | 0.0% — **infra artifact**, not a real loss |
| main | 17/93 | 9.4% |

## The governing caveat: availability ≠ proven adequacy

Everything above is *mechanical availability*. None of the 19 successes are
quality-judged against the deep tier, and per ROADMAP the main-turn quality
judge is an explicitly deferred open question. So absorption-**with-proven-
adequacy** (the actual win condition 1) is computable today only for the
`chore` + `compaction` slice; the `main` share of that 9–18% is availability,
not yet earned. (Corroborating but not conclusive: on the 19 both-served
captures local emitted *more* completion tokens than deep — 12,920 vs 10,010 —
and 13 of 19 were tool-call turns whose tool-call validity is unchecked here.)

## Next actions

1. Re-run the 12 infra-failed captures (list below) with the MLX server
   confirmed up, to collapse the 9.4%–18% range to a point.
2. Stand up the adequacy pass: mechanical for `compaction`, hand-judged for
   `main`, to convert "served" into "adequately served."
3. Grow the organic corpus past 96 (real sessions with capture on) — 93/2/1 is
   thin for the chore and compaction classes where local actually wins.

### The 12 captures to re-run

```
req-20260810T134326.900415-0007.json   main        HTTP 500   14384
req-20260810T175135.843443-0003.json   main        HTTP 500   20469
req-20260810T175948.107483-0006.json   main        HTTP 500   27897
req-20260810T180003.437719-0009.json   main        HTTP 500   29083
req-20260810T180015.708753-0011.json   main        HTTP 500   29942
req-20260810T180225.034251-0013.json   main        HTTP 500   30452
req-20260810T180350.214840-0014.json   main        HTTP 422   30476
req-20260810T180402.287022-0016.json   main        HTTP 500   30940
req-20260810T180423.543757-0017.json   main        HTTP 422   30733
req-20260810T180644.925750-0019.json   main        HTTP 500   31230
req-20260810T180647.883035-0020.json   main        HTTP 422   31293
req-20260810T182441.283667-0087.json   compaction  HTTP 500   35183
```

## Commands

```
# frozen corpus (organic only), self-feed guard live in the router
python3 scripts/replay_captures.py ~/loxo-corpus-frozen --out replay-results.jsonl

# re-run only the 12: strip their rows so the harness re-issues them,
# with the MLX server up
grep -vFf twelve-captures.txt replay-results.jsonl > tmp && mv tmp replay-results.jsonl
python3 scripts/replay_captures.py ~/loxo-corpus-frozen --out replay-results.jsonl
```
