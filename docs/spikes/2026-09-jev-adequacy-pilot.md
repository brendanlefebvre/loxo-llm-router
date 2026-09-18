# Spike: Jev adequacy pilot — can a System One model drive the dial? (2026-09)

**Question:** TypeSafe's Jev (a "System One" decision model: typed judgments +
probabilities, no text generation) launched 2026-09-16. If its calibrated
probabilities are real on loxo's traffic, the dial stops being class-heuristic
plumbing and becomes a threshold over `p(local adequate)` asked per request.
This pilot measures that directly: **do Jev's adequacy probabilities carry
signal on real captured traffic?** Secondary: can Jev replace `classify()` as
the labeling instrument?

**Method:** one Jev call per capture over the frozen 96-capture corpus (the
2026-08 local-absorption corpus, unchanged), carrying two parallel questions:

- `local_adequate` (noul): *"Assume the prompt fits within the model's context
  window. Would a 14-billion-parameter open-weights local model (Qwen3-14B
  class, 4-bit quantized) produce an adequate response to the final user
  message…?"* with criteria describing shippable vs. failing outputs.
  The fits-assumed framing is deliberate: context-fit is mechanical and stays
  loxo's job; letting Jev count tokens would inflate its measured skill with
  easy negatives.
- `cls` (choice): main / chore / compaction / other, criteria mirroring
  `classify()`'s semantics but with no fingerprint knowledge.

Ground truth: the local tier replayed fresh (forced `loxo/local` →
mlx-community/Qwen3-14B-4bit @ 40,960, temperature 0, sequential), and every
served output hand-judged adequate/inadequate against its transcript.
Negative control: the same questions over word-scrambled state (semantics
destroyed, length/vocabulary kept).

Cost of the entire Jev side, both passes: **≈ $0.03** (input $0.042/Mtok,
output free), ~0.7 s median latency per call.

## Constraint discovered first: the 32k state ceiling

Jev's context is 64k with **state capped at 32k tokens** (undocumented on the
API page; surfaced as HTTP 400 `max_tokens_exceeded`). Only **19 of 96**
captures fit — JSON-encoded messages plus tool schemas inflate past the raw
prompt size. The cap almost coincides with the local 40k context, so the
captures Jev cannot see are, to first order, the ones the mechanical overflow
guard already handles without it. But it also means: on this hardware class,
**a Jev-driven dial could only ever adjudicate the frontier that fits both
windows**, and the graded sample here is n=19 (15 served, 4 GPU-OOM).

## Result 1: classification — perfect on the answered subset

**19/19 agreement with `classify()`** (17 main, 2 chore), from semantics alone,
no fingerprints. On this evidence Jev could do the offline labeling job today.
Caveat: the corpus's 93/2/1 class skew gives near-zero resolution on the rare
classes.

## Result 2: adequacy — no measurable signal

Hand-judged adequacy of the 15 served outputs: 13 adequate, 2 inadequate
(base rate 0.867). Jev's predictions all sit in a hedged **0.45–0.72 band**
(median 0.58). Against the labels:

| metric | Jev | constant base-rate | coin flip (0.5) |
|---|---:|---:|---:|
| Brier (capability, n=15) | 0.201 | **0.116** | 0.250 |
| Brier (practical, n=19, OOM→inadequate) | 0.229 | **0.216** | 0.250 |
| AUC (capability) | **0.500** | — | — |
| AUC (practical) | 0.583 | — | — |

Mean p by bucket: adequate **0.587**, inadequate **0.600**, OOM **0.575** —
the inadequate outputs scored slightly *higher* than the adequate ones.
AUC 0.500 is zero discrimination. The pre-stated gate ("beat the base-rate
predictor on Brier") is failed on both framings.

**Negative control: weak pass.** Scrambled state moved p toward uninformative
on 17/19 paired captures (median 0.58 → 0.50, mean Δ −0.083) — Jev reads
semantics, not just length. Class stayed confidently correct on scrambled
text, which is expected (scrambling preserves the vocabulary that identifies
a harness prompt) rather than damning. But the organic-to-scrambled shift is
about the same size as the entire organic spread, which is the geometry of a
model that has *some* signal about the state and *none* about the question
asked of it.

**Judging note (bias direction: lenient).** Two initial misjudgments were
caught only by reading full transcripts — a seeming Task-1 repetition loop
that was actually correct recovery from a failed subagent dispatch (the
harness had rejected an invalid `subagent_type`). Single-turn eyeballing
under-rates local; per-turn "acceptable to ship" is a lenient bar. Both push
the base rate up, which makes Jev's miss *smaller* than it looks, not larger.

## Secondary finding: OOM attrition is worse than August

This replay landed **21 served / 65 overflow / 10 OOM** vs. August's
28/65/2(+1). The extra OOMs cluster in the same 30k-band and the same
captures served in August, reconfirming path-dependence — but today's crash
*rate* is far higher. The multi-model-residency hypothesis was checked and
**refuted**: the server log shows only the 14B ever loads (the five-model
`/v1/models` listing is HF-cache inventory, not residency), and the weights
are a normal 7.8 GB. The measured story instead: `footprint -p <pid>` shows
the server's `IOAccelerator` (Metal wired) category pinned at **18 GB —
exactly `iogpu.wired_limit_mb`** — while RSS reads a useless 1 GB. MLX's
Metal buffer pool retains peak KV allocations, so back-to-back 30k prefills
on a long-lived server run at the wired ceiling, where any fresh allocation
can die. August's runs were babysat with restarts (empty cache); today's
gated run plowed through the big captures consecutively. Consistent with the
August finding that the compaction capture served only on a clean cache.
Mitigation for future replays: restart MLX between heavy batches, or teach
the health-gate to bounce the server when footprint nears the wired limit.
Diagnostic recipe, since RSS is blind to all of this: `footprint -p <pid>`
for Metal wired usage, the server log for actual model loads, and
`sysctl iogpu.wired_limit_mb` for the ceiling.

Also reconfirmed operationally: one OOM triggers a slow 14B reload during
which every subsequent request 422s (`local_unreachable`) — a naive
sequential run turns one crash into a 20-row cascade. The replay harness needs
an MLX health-gate between requests (added to the throwaway runner here;
worth porting to `scripts/replay_captures.py`).

## Verdict

- **Jev as adequacy oracle (the dial): not yet.** Zero discrimination and
  worse-than-base-rate calibration on this corpus, judged at n=15/19. The
  dial's evidence source remains loxo's own adequacy ledger — observed
  outcomes, not predicted ones.
- **Jev as classifier/labeler: yes, on this evidence.** 19/19, semantics-only,
  ~$0.0002/label where it fits. The 32k cap limits it to the same frontier
  the dial cares about, which is exactly where `classify()`'s fingerprints are
  also weakest.
- The negative result is bounded, not final: n is small, the question wording
  was one attempt (a Score rubric over failure modes, richer criteria, or
  decomposed judgments per the composite-scoring pattern were not tried), and
  Jev is a day-2 product. A follow-up with an expanded corpus (the ~230 newer
  captures, artifact-filtered) and 2–3 question formulations would put
  ~4× the labels behind whichever way the next measurement points.

**Raw data** (results JSONL for both sides, judgments with rationales, the
throwaway runners): `$LOXO_STATE_DIR/spikes/2026-09-jev/` on the machine that
ran the pilot. Not committed — prompts are raw operator content.
