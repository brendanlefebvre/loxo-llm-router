# Loxo Roadmap: Harness Parity and the Local/Frontier Dial

Status: Reviewed (all sections approved 2026-07-15); execution proceeds via
per-milestone implementation plans, v0.2 first
Date: 2026-07-15

## Section 1: North star and win/loss conditions

**North star — an aspiration, not a description.** None of what follows in
this paragraph is true today, and some of it may never be; the roadmap exists
to close each gap or to establish, with evidence, why it cannot be closed.
The aspiration: Loxo becomes the dial that tracks the local/frontier boundary
as it moves. Concretely: you point OpenCode or Pi at Loxo with an OpenRouter
key (and optionally a local server URL); the session feels foundation-quality
from turn one; the cost lands at or below what the operator actually spends
today; and local models absorb an evidence-earned, growing share of the
traffic without the session ever feeling it.

**Where Loxo actually stands (v0.1.0):** an OpenAI-compatible proxy with
heuristic tier routing, local→cloud transport fallback, a vision policy, a
spend ledger, and a live rate card. No cache injection, no reasoning
passthrough, no request classification, no adequacy measurement, no dial.
The distance between that paragraph and this one is the roadmap.

**Win conditions** (checkable, in priority order):

1. **Capability**: a real working session in OpenCode and in Pi, on `loxo/*`
   tiers, doing real work, doesn't feel handicapped versus the native
   foundation stack — backed by a parity scorecard with per-capability
   evidence, not vibes alone.
2. **Cost**: `/v1/spend` for a representative month is at or below the
   operator's actual current monthly spend. Parity is a win; meaningfully
   more expensive for a similar experience is *the* loss condition.
3. **The dial exists**: per-request-class local share is measured, visible,
   and expands when evidence supports promotion — the capability neither the
   foundation harnesses (no local incentive) nor OpenRouter (no local access)
   can ever offer.

**Explicit non-goals for this roadmap:**

- Claude Code as a client (no Anthropic `/v1/messages` endpoint) — reassessed
  at the harness checkpoint.
- Spec-completeness for arbitrary OpenAI-compatible harnesses. OpenCode + Pi
  are the acceptance tests; fixes are implemented at the dialect level (no
  user-agent sniffing), so broad compatibility falls out as a byproduct, but
  the promise stays bounded to the named harnesses.
- Automatic, unsupervised promotion of traffic classes to local routing.
  Promotion changes which model silently serves a class of requests; a wrong
  promotion is an invisible quality regression — the same failure shape as a
  fallback that masks a real error. The router's job is to *recommend*
  promotions with evidence; a human flips the switch. (Automation can be
  revisited once the adequacy metrics have proven trustworthy.)

**The honesty mechanism.** The parity scorecard has a standing section for
gaps that are *structurally* unclosable — e.g., if the real cost baseline is
subsidized subscription pricing that per-token routing mathematically cannot
match, or harness-side prompt tuning that no router can compensate for.
"We can say for sure why it's not possible" is a first-class deliverable of
this roadmap, not a failure state.

## Section 2: Roadmap architecture — two tracks, three milestones

The roadmap runs two tracks in parallel. The **parity track** is the visible
work: closing the capability gaps between "OpenCode/Pi through Loxo" and the
native foundation stacks. The **dial track** is the quiet work: shipping
classification and measurement in observe-only mode from the first release,
so that evidence starts accruing immediately — the dial's premise is
evidence, and evidence has lead time. Every release advances both.

### Track A — Parity (cloud-side capability)

- **A1. Prompt cache injection.** OpenAI-compatible harnesses never send
  Anthropic `cache_control` breakpoints, so Loxo injects them on cloud-bound
  requests whose target supports caching: system prompt, tool definitions,
  and a stable conversation prefix (respecting the 4-breakpoint limit).
  This is the single largest cost lever for agentic traffic (cached input
  tokens are ~10x cheaper). Cache hit rates surface in `/v1/spend`.
  Constraint to verify: cache affinity through OpenRouter may require
  provider pinning.
- **A2. Reasoning support.** Tiers gain a reasoning knob mapped to
  OpenRouter's `reasoning` parameter; reasoning deltas stream through to the
  client. Verified against what OpenCode and Pi actually render.
- **A3. Model metadata honesty.** A virtual tier has no single static cost,
  so the two metadata consumers are answered differently:
  - *Context length* is well-defined per tier by its routing policy: a tier
    with cloud escalation effectively has its cloud target's context window
    (oversized prompts escalate by rule); a hard-pinned local tier has the
    local context limit. `/v1/models` advertises these, kept truthful
    against the live rate card rather than hand-maintained.
  - *Price* is advertised as a **ceiling** (the tier's cloud-target rate —
    the conservative direction: harness estimates can only err high), while
    per-call truth flows dynamically: `usage.cost` passes through on every
    cloud response and local-served calls report cost 0. Static metadata
    says "at most this"; `/v1/spend` says what actually happened. The gap
    between ceiling and actual is the dial's value, made visible.
- **A4. Parity scorecard.** A living `docs/parity.md` scoring Loxo against
  the native stack per capability (caching, reasoning, tool fidelity,
  vision, metadata, streaming, error surfaces, cost) with evidence links.
  Includes the standing "structurally unclosable" section.

### Track B — The dial (measurement before movement)

- **B1. Request classification.** Heuristic, no harness cooperation
  required: each request is classified from observable shape (tool
  presence/count, message count, prompt size, system-prompt fingerprint,
  streaming flag). Taxonomy starts minimal: `main`, `chore`
  (title/summarize), `compaction`, `unknown`. Growing the taxonomy is
  cheaper than shrinking a wrong one.
- **B2. Adequacy ledger.** A JSONL twin of the spend ledger. Per request:
  class, route taken, model, and outcome signals — tool-call JSON validity,
  finish reason, HTTP status, whether fallback fired, latency, token counts.
- **B3. Observe-only first.** B1+B2 ship changing no routing whatsoever.
- **B4. Counterfactual economics — two directions, two mechanisms.**
  - *Local-served → cloud price* is arithmetic: token counts × the tier's
    cloud-target rate card = what the call would have cost. Reported as
    savings.
  - *Cloud-served → local adequacy* cannot be observed from unserved
    traffic. The mechanism is **shadow evaluation**: for candidate classes,
    a sampled fraction of cloud-bound requests is also sent to local in the
    background — fire-and-forget, non-streaming, off the critical path,
    never touching the user-facing response — and scored on mechanical
    adequacy signals (valid tool-call JSON, schema conformance, sane finish
    reason) into the adequacy ledger with a `shadow` flag. Shadowing is
    opt-in, rate-limited, and per-class: local tokens are free but the
    compute runs on the operator's own machine.
  - *Known limitation (stated in the scorecard):* mechanical signals measure
    well-formedness, not answer quality. Shadowing therefore starts with
    classes where the two coincide (titles, summaries, compaction);
    quality-judging for main-turn classes is explicitly future work.
- **B5. The dial surface.** A `/v1/dial` endpoint (and scorecard section)
  reporting per-class volume, local share, adequacy stats (live and
  shadow), and both counterfactual directions from B4.
- **B6. Earned promotion.** Per-class routing lives in `loxo.toml`;
  `/v1/dial` produces promotion recommendations once a class accumulates N
  shadow samples above an adequacy threshold, with the evidence attached; a
  human edits the config. (Per Section 1, promotion is never automatic.)
  Concrete values for N and the threshold are set in the v0.3 implementation
  plan, informed by the request volumes the v0.2 ledger actually observes.

### Milestones

- **v0.2 — "Honest cloud."** A1 cache injection, A3 metadata, scorecard v1,
  B1+B2 observing. Acceptance: a real OpenCode session on `loxo/*` shows
  cache hits in `/v1/spend`, correct context windows, and a populating
  adequacy ledger.
- **v0.3 — "Visible dial."** A2 reasoning, B4 shadow evaluation on chore
  classes, B5 `/v1/dial`, first human-flipped chore-class promotion.
  Acceptance: measurable local share on at least one class, defended by
  shadow-derived adequacy stats.
- **v0.4 — "Earned share."** B6 promotion recommendations, Pi acceptance
  run, scorecard v2, and **harness checkpoint #1**: reassess which harnesses
  are worth targeting (including whether Claude Code via a `/v1/messages`
  endpoint has become worth it).
- **Cadence.** Each subsequent minor release repeats the loop: parity gaps
  found by real sessions, dial share expanded by evidence, harness set
  reassessed.

## Section 3: Evidence, testing, and risks

### Code structure

The router is a single 1,055-line `__init__.py`; this roadmap adds a
classifier, a cache injector, an adequacy ledger, shadow evaluation, and a
dial endpoint. Rather than grow the monolith, each lands as its own module
with one clear seam (following the `config.py` precedent): `classify.py`,
`cache.py`, `ledger.py` (absorbing the existing spend accumulator), `dial.py`.
The routing core (`pick_target`, `forward`) stays put and stays small.

### Testing strategy

- **Unit tests** (existing pytest style): classifier taxonomy decisions,
  cache-breakpoint placement (deterministic given a body), tier metadata
  synthesis, counterfactual arithmetic.
- **Golden harness fixtures** — the harness-as-spec made executable. Capture
  sanitized real request bodies from OpenCode and Pi sessions (main turns,
  title/summarize chores, compaction, tool-heavy turns), pinned per harness
  version. Replay them against the app with a stubbed upstream; assert
  routing decisions, injected fields (`cache_control`,
  `stream_options.include_usage`), streaming semantics, and error surfaces.
  When a harness updates and its request shapes drift, the fixtures say so
  before a user session does.
- **Live smoke + acceptance session.** A scripted end-to-end check against
  real OpenRouter (cheap model) and a local server, plus — per milestone —
  one real working session in each target harness. The acceptance session is
  the only test that can judge "doesn't feel handicapped."
- **The ledgers as longitudinal tests.** The adequacy ledger's
  `unknown`-class rate is a standing metric of classifier health; a spike
  means a harness changed shape.
- **CI**: GitHub Actions running unit + fixture tests on every PR (the repo
  currently has no CI; this roadmap makes it necessary, not optional).

### Risk register ("why it might not be possible" candidates)

1. **Subscription economics.** If the operator's real baseline is a
   subsidized flat-rate plan (Claude Max class), per-token routing may be
   structurally unable to match it for heavy usage. *Disposition: the
   scorecard's cost section answers this with a month of real data —
   producing that answer is a win condition, not a failure.*
2. **Cache affinity through OpenRouter.** Anthropic cache hits require
   consecutive requests to land on the same provider; OpenRouter's load
   balancing may break affinity without provider pinning. *Disposition:
   spike this first in v0.2 — if caching through OpenRouter proves
   unreliable, the cost leg of the vision needs redesign, and we want to
   know in week one.*
3. **Classifier fragility.** Heuristic classification breaks when harnesses
   change their request shapes between versions. *Disposition: observe-only
   start, `unknown` as the safe default class, per-version golden fixtures.*
4. **Harness-side tuning gap.** Claude Code's prompts are tuned for Claude;
   OpenCode/Pi are model-agnostic. Some experience gap lives in the harness
   and no router can close it. *Disposition: named and measured in the
   scorecard rather than silently absorbed as Loxo's failure.*
5. **Shadow load on the working machine.** Shadow evaluation competes for
   the same GPU/memory as the operator's interactive local traffic.
   *Disposition: opt-in, per-class sampling rates, and a kill switch;
   idle-aware scheduling is future work.*
6. **Local server variance.** Local OpenAI-compatible servers differ in
   usage reporting and tool-call fidelity. *Disposition: token counts fall
   back to estimation when usage is absent; adequacy signals are computed
   from the response body, not trusted fields.*
7. **Ledger privacy.** Ledgers record metadata (class, model, counts,
   outcomes), never message content; shadow evaluation sends content only to
   the local backend, adding zero new cloud exposure.

### Open questions (deferred, tracked here so they aren't lost)

- Quality-judging for main-turn classes (LLM-judge comparison of shadow
  outputs) — required before the dial can ever move main-turn traffic.
- Idle-aware shadow scheduling.
- Whether `/v1/messages` (Claude Code as client) enters scope at harness
  checkpoint #1.
- Automatic promotion, revisited only after adequacy metrics have a track
  record a human has learned to trust.
