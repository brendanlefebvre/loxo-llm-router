# Spike: context curation and the closing cheap lane (2026-07)

> **STATUS: PLAN, NOT RESULTS.** Nothing below has been measured. Every number is a
> proposed threshold, not an observation. Do not cite this document as evidence of
> anything until the Results sections are filled in.

**Question:** the router currently decides *a priori* — it picks a lane from the
request before seeing how it goes. In a running conversation, context grows
monotonically, so the cheap lane closes over time **regardless of whether the task
got harder**: by turn 30 a 7B model is ineligible because the envelope is too big,
not because it couldn't do the work. Does constructing a *curated payload* per task
(rather than forwarding accumulated conversation state) keep the cheap lane open —
and if so, who builds the payload?

That last part is a fork with real consequences, so it gets decided by evidence:

- **Client constructs the brief** (subagent-style) — reliable, but requires client
  cooperation, which breaks the drop-in OpenAI-compatible property that is currently
  part of the router's value.
- **Router auto-selects from history** — preserves drop-in, but the router is now
  making semantic judgments about relevance, and a wrong elision produces a
  *confidently wrong* cheap answer. Curation also costs a model call, which eats the
  savings being chased.
- **Hybrid** — the protocol lets callers tag task-relevant turns; the router honors
  tags when present and routes conservatively when absent.

Full transparency and full curation are not simultaneously available. This spike
exists to pick deliberately rather than discover the tradeoff later.

## Design rules

These two constraints are what make this a weekend rather than a research program.
Violating either is how the spike dies half-finished.

1. **Grade mechanically.** Only use tasks with objective pass/fail — code where a
   test suite decides the outcome. Candidate corpora with real suites already in
   place: pick two or three local repos with mature suites — one with a few dozen
   tests is ideal, since it gives unambiguous pass/fail per task. Hand-judging
   outputs does not scale past about a dozen tasks and the spike will stall.
2. **Use real tasks, not synthetic ones.** Mine `~/.claude-memory/conversations.db`
   — the claude-memory plugin's recall archive (Stop-hook populated; schema
   `projects → sessions → branches → messages`, with FTS5 indexes over messages and
   branches). Synthetic tasks flatter curation, because a hand-written task is
   unconsciously written self-contained — which is precisely the property under test.

   **Two caveats, both load-bearing:**

   - **This is not a log of routed traffic.** It records *Claude Code* sessions,
     which talk to Anthropic directly and never traverse the router. Loxo serves
     OpenCode and similar clients; the two sets are effectively disjoint. The only
     true routing telemetry is the spend ledger. What `conversations.db` provides is
     a corpus of *realistic multi-turn conversations* — which is what Exp 0a and task
     mining actually need, since those turn on conversation shape rather than on
     which lane served the request.
   - **It is per-machine, and most machines are the wrong machine.** Each host keeps
     its own DB, and they are wildly uneven — on one server here, ~80% of sessions
     belong to a single non-development project, leaving only a handful per code repo.
     Check the `cwd` distribution (`select cwd, count(*) from sessions group by cwd`)
     before choosing a source, and mine whichever host actually carries the
     development traffic.

**N = 20–30 tasks** is sufficient for go/no-go. This is a decision aid, not a
publication.

### 3. Never continue a recorded conversation — freeze it and ask once

A conversation is **jointly constructed**: every user turn is conditioned on the
specific assistant turn preceding it. Substitute a different model and the recorded
user turns begin responding to things that never happened ("yes, do the second one"
when no options were offered), and the divergence *compounds* turn over turn. This is
the off-policy evaluation problem and it has no clean workaround.

The design therefore never re-enacts a dialogue. **Using a transcript as context is
not the same as continuing a conversation.** Freeze the transcript up to turn N as an
immutable blob — exactly as recorded, both halves — then pose turn N+1's task **once**,
varying only the treatment:

| Arm | Context | Model |
|---|---|---|
| A | full transcript | local |
| B | curated payload | local |
| C | full transcript | frontier (ceiling / reference) |

Exactly one new generation occurs, at the end. Divergence would require the recorded
user turns to respond to newly generated assistant turns; that never happens, because
nothing after the frozen point comes from the transcript.

Grade on **outcome, not trajectory match** — a different route to green tests is fine,
arguably better. This is why mechanical grading (rule 1) is load-bearing rather than a
convenience: it makes the whole spike outcome-based, so divergence in *approach* is
irrelevant by construction.

This is **SWE-bench's methodology** (repo at commit N-1 plus a goal, graded by tests at
commit N, never replaying a conversation). Steal the shape; it is a validated answer to
this exact problem.

**Two caveats on this design, both to check before committing to it:**

- **The mechanically-gradable intersection may be thin.** The design needs conversations
  whose turn N+1 produced a commit in a repo carrying a test suite — a narrower set than
  "all conversations," and possibly under N=20 in practice. Measure the intersection size
  first. Fallbacks: derive tasks from commit history directly and use a real transcript as
  context ballast, or accept hand-grading a smaller N.
- **There is a selection effect, and it must not be laundered.** Tasks requiring genuine
  interactive back-and-forth are exactly the tasks curation cannot serve, so excluding
  them is scoping to the target population rather than cherry-picking. But the resulting
  number reads as *"curation works on curatable tasks"* and does **not** generalize to all
  traffic. Exp 0a/0b size that population. Report the two numbers separately.

## Sequencing note (data availability)

The ledger does not yet hold enough traffic to answer Exp 0b. **Do not serialize on
it.** Instrument now, let it accumulate passively, and run Exp 1 meanwhile — Exp 1
depends only on `conversations.db`, which is already populated.

    Day 0:  add ledger fields (Exp 0b instrumentation)  ──┐
    Day 0:  run Exp 0a (retrospective, no new data)       │  accumulating
    Day 0+: run Exp 1 (ceiling test)                      │
    Day 7+: run Exp 0b once traffic has built up        ──┘

## Experiments

Ordered by cost and by likelihood of killing the idea. Cheapest killer first.

### Exp 0a — is the problem real? (retrospective, zero new data)

**Method:** the size question is answerable *today* without any instrumentation,
because it is about token counts rather than routing decisions. Walk the `messages`
table per session in timestamp order (threading via `parent_uuid`), accumulate
content length, approximate tokens (`len // 4`, per the cache-affinity spike's
convention), and count what fraction of turns would have exceeded the local model's
context window.

**Check first — possible shortcut:** `conversations.db` carries a `token_snapshots`
table whose columns are exactly what this experiment wants (`input_tokens`,
`output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, per session) — but on
the host checked here it holds **zero rows**, i.e. the schema exists and nothing
populates it.
If that can be enabled, Exp 0a stops being a hand-rolled token-accounting job. Worth
ten minutes of investigation before writing any replay code.

**Metric:** % of turns, and % of *sessions*, that outgrow the local window; the turn
index at which sessions typically cross it.

**Kill criterion:** if conversations rarely outgrow the local window in practice,
the premise is false and the whole spike stops here.

### Exp 0b — is size actually what routes traffic to cloud? (needs instrumentation)

**Method:** add two fields to the spend ledger — **prompt token count** and
**routing reason** (size-driven vs difficulty-driven). Accumulate real traffic, then
attribute every cloud-routed request to one cause or the other.

**Metric:** share of cloud routes that are size-driven.

**Kill criterion:** **under ~25% size-driven → stop.** If the expensive lane is
carrying genuinely hard tasks rather than merely large ones, curation is solving a
marginal problem and everything below is wasted effort.

### Exp 1 — the ceiling test (highest value; run early)

**Method:** take 20–30 real tasks that were routed to cloud. **Hand-curate** a
minimal payload for each — deliberately, timeboxed to ~10 minutes each. Run the
local model against the hand-curated payload. Grade mechanically against the known
outcome.

Hand-curation is the **upper bound**: no automatic curator will beat a human doing
it carefully with full knowledge of the task.

**Kill criterion:** **under ~50–60% success on tasks the cloud model solved → stop.**
The ceiling is too low and no clever curator rescues it.

**Why this one matters most:** it is the only experiment that separates *"curation is
impossible"* from *"my curator is bad."* The second cannot be diagnosed without
first establishing the first. Running the curator before the ceiling test produces an
uninterpretable result.

### Exp 2 — how much context is load-bearing?

**Method:** on tasks where Exp 1 succeeded, ablate the payload — strip turns, strip
code, strip error detail — and binary-search the minimum viable payload.

**Metric:** fraction of the original conversation actually required.

**Reading it:** ~5% → curation is enormously valuable. ~60% → marginal, and the
engineering cost probably is not worth it.

This experiment also produces the curator's specification *from evidence* rather than
intuition: whatever survives ablation is what the curator must preserve.

### Exp 3 — the dumb baseline (run before anything clever)

**Method:** mechanical rules, zero model calls — last N turns + file currently under
edit + original task statement + last error. Compare against the Exp 1 hand-curated
baseline.

**Decision:** if mechanical reaches ~80% of hand-curated performance, **build that
and stop.** The trivial baseline is tested first precisely because it is frequently
embarrassing how well it does, and a smart curator that cannot beat it is pure
liability.

### Exp 4 — the crux: can curation be cheap?

This experiment decides the architectural fork in the Question section.

**Load-bearing hypothesis:** *selecting relevant context is a strictly easier task
than solving the problem.* If true, a small model can curate for itself, router-side
auto-curation works, and drop-in compatibility is preserved. If false, client-side
tagging is the only viable path and the transparent-proxy property has to be traded
away.

**Method:** have the local model produce the curated payload (given full conversation
plus task), then have the local model solve from *its own* payload. Compare to the
hand-curated baseline.

**The trap:** if the *frontier* model is needed to curate, the entire purpose is
defeated. Curation must be cheap or it is not curation. Measure the curation call's
own token cost and count it against the savings.

## Decision tree

| Outcome | Decision |
|---|---|
| Exp 0a — conversations rarely outgrow the local window | **Stop.** Premise false. |
| Exp 0b — under ~25% of cloud routes are size-driven | **Stop.** Not the bottleneck. |
| Exp 1 — ceiling under ~50–60% | **Stop.** Fidelity cannot survive curation. |
| Exp 2 — most context is load-bearing | **Stop.** Nothing meaningful to strip. |
| Exp 3 — mechanical ≈ hand-curated | **Ship the mechanical curator.** Skip the clever one. |
| Exp 4 — model-curated ≈ hand-curated | **Build router-side auto-curation.** Drop-in preserved. |
| Neither 3 nor 4 reaches the ceiling | **Client-tagged hybrid only.** Then decide whether breaking drop-in is worth it. |

## Cost guards

Following `scripts/cache_spike.py` convention, any script written for this spike
carries a `--max-usd` guard and a `--dry-run` that reports token counts without
issuing calls. Exp 0a, 0b, and 3 should cost **\$0.00** in API spend (local and
analysis only). Exp 1 and 4 involve frontier-model grading comparisons; budget a
guard of **\$5.00** total across the spike and record actual spend here when the
Results sections are filled in.

## Relationship to other work

- **Litmus** is the grading harness. "Did the curated payload lose something
  important?" is an eval question, and the answer is measured rather than asserted.
  Curation fidelity becomes a Litmus-shaped metric.
- **The OTel trace-emitter** (spec on `feat/otel-trace-emitter`, unimplemented) gets
  a far more compelling first use case here than generic tracing: emitting
  per-request curation and escalation decisions with their token costs is exactly the
  telemetry these experiments consume.
- **Escalation / fall-forward routing** is the sibling idea — escalate on observed
  failure carrying the failure trace, rather than predicting difficulty up front.
  Curation and escalation compose: curated task payloads make escalation affordable,
  because escalating sends a small focused payload rather than an entire conversation.
  Both are instances of one principle — **construct the payload, do not forward
  accumulated state.**

## Implications if this lands

The router stops being a proxy that picks a lane and becomes a layer that decides
*what context a task needs, who can serve it at that size, and what curation cost in
fidelity* — with each of those three backed by a measurement rather than a heuristic.
That is a materially harder thing to replicate than dispatch rules, and it is on-thesis
for the eval-driven positioning.

If it does not land, the kill criteria above should make that clear within about a
week, at close to zero API spend, which is the point of ordering them this way.
