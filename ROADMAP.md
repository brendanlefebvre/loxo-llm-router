# Roadmap: Harness Parity and the Local/Frontier Dial

Adopted 2026-07-15/16, against a v0.1.0 baseline. This document holds *intent* — where [ARCHITECTURE.md](ARCHITECTURE.md) describes only what's implemented today, this describes what each release is closing the gap toward, and why. PRs migrate decisions from here into ARCHITECTURE.md as features actually land.

## North star

**An aspiration, not a description.** None of what follows in this section is true today, and some of it may never be; the roadmap exists to close each gap or to establish, with evidence, why it cannot be closed.

Loxo becomes **the dial that tracks the local/frontier boundary as it moves.** Concretely: you point OpenCode, Oh-My-Pi, or Claude Code at Loxo with an OpenRouter key (and optionally a local server URL); local models absorb an evidence-earned, growing share of a real agentic workload; the cost lands at or below what the operator actually spends today; and the session stays good enough that choosing Loxo over the native stacks never feels like a sacrifice.

**Where Loxo actually stands (v0.1.0):** an OpenAI-compatible proxy with heuristic tier routing, local→cloud transport fallback, a vision policy, a spend ledger, and a live rate card. No cache injection, no reasoning passthrough, no request classification, no adequacy measurement, no dial. The distance between that paragraph and this one is the roadmap.

## Win conditions

Checkable, in priority order:

1. **Local absorption** — the headline metric: the fraction of real-session tokens served locally *with proven adequacy*, growing release over release. Per-request-class local share is measured, visible, and expands only when evidence supports promotion — the capability neither the foundation harnesses (no local incentive) nor OpenRouter (no local access) can ever offer.
2. **Cost** — `/v1/spend` for a representative month is at or below what the operator actually pays today on **metered, per-token pricing**. The baseline is deliberately metered spend: against a subsidized flat-rate subscription (Claude Max class) under heavy use, per-token routing cannot win by arithmetic, so that case is out of scope for the cost win and said plainly in the README's "Who this is for" (risk 1 still measures it with real data). Parity is a win; meaningfully more expensive for a similar experience is *the* loss condition.
3. **Experience floor** — a constraint with teeth, not a vestige: a real working session in OpenCode and in Oh-My-Pi (omp), on `loxo/*` tiers, must stay above "I'd still choose this for real work today," backed by the parity scorecard with per-capability evidence. The floor protects the evidence stream: a dial with no traffic measures nothing — local absorption of zero sessions is zero.

**Sequencing honesty:** near-term movement on conditions 1–2 concentrates in the chore classes (titles, summaries, compaction) — exactly the classes where mechanical adequacy signals suffice (A4). Main-turn traffic, where spend actually concentrates, stays cloud until main-turn quality-judging exists (an explicitly deferred open question). First sessions should expect chore-class savings, not big-ticket ones.

## Explicit non-goals

- Serving consumer-OAuth credentials. Claude Code *does* enter scope as a client via an Anthropic `/v1/messages` endpoint (B5 — harness checkpoint #1, decided 2026-07-20; see Milestones), but strictly for API-key-pattern clients: Anthropic blocks consumer OAuth tokens (Pro/Max subscriptions) outside its own products, and Loxo will never implement workarounds. That population is already outside the cost win's audience (win condition 2), so the terms boundary and the positioning boundary are the same line.
- Spec-completeness for arbitrary OpenAI-compatible harnesses. OpenCode + Oh-My-Pi are the acceptance tests; fixes are implemented at the dialect level (no user-agent sniffing), so broad compatibility falls out as a byproduct, but the promise stays bounded to the named harnesses.
- Automatic, unsupervised promotion of traffic classes to local routing. Promotion changes which model silently serves a class of requests; a wrong promotion is an invisible quality regression — the same failure shape as a fallback that masks a real error. The router's job is to *recommend* promotions with evidence; a human flips the switch. (Automation can be revisited once the adequacy metrics have proven trustworthy.)

## The honesty mechanism

The parity scorecard (Track B, below) keeps a standing section for gaps that are *structurally* unclosable — e.g. if the real cost baseline is subsidized subscription pricing that per-token routing mathematically cannot match, or harness-side prompt tuning that no router can compensate for. "We can say for sure why it's not possible" is a first-class deliverable of this roadmap, not a failure state.

## Roadmap architecture: two tracks, four milestones

The roadmap runs two tracks in parallel. The **dial track** is the headline work: classification, adequacy measurement, shadow evaluation, and earned promotion — the machinery of local absorption, shipping observe-only from the first release because the dial's premise is evidence and evidence has lead time. The **parity track** is the floor work: closing the cloud-side capability gaps that keep the experience worth choosing (win condition 3) and the cost comparison honest (win condition 2). Every release advances both. (Track A is the dial because local absorption is win condition 1; Track B is parity, the floor.)

### Track A — The dial (measurement before movement)

- **A1. Request classification.** Heuristic, no harness cooperation required: each request is classified from observable shape (tool presence/count, message count, prompt size, system-prompt fingerprint, streaming flag). Taxonomy starts minimal: `main`, `chore` (title/summarize), `compaction`, `unknown`. Growing the taxonomy is cheaper than shrinking a wrong one.
- **A2. Adequacy ledger.** A JSONL twin of the spend ledger. Per request: class, route taken, model, and outcome signals — tool-call JSON validity, finish reason, HTTP status, whether fallback fired, latency, token counts.
- **A3. Observe-only first.** A1+A2 ship changing no routing whatsoever.
- **A4. Counterfactual economics — two directions, two mechanisms.**
  - *Local-served → cloud price* is arithmetic: token counts × the tier's cloud-target rate card = what the call would have cost. Reported as savings.
  - *Cloud-served → local adequacy* cannot be observed from unserved traffic. The mechanism is **shadow evaluation**: for candidate classes, a sampled fraction of cloud-bound requests is also sent to local in the background — fire-and-forget, non-streaming, off the critical path, never touching the user-facing response — and scored on mechanical adequacy signals (valid tool-call JSON, schema conformance, sane finish reason) into the adequacy ledger with a `shadow` flag. Shadowing is opt-in, rate-limited, and per-class: local tokens are free but the compute runs on the operator's own machine.
  - *Known limitation (stated in the scorecard):* mechanical signals measure well-formedness, not answer quality. Shadowing therefore starts with classes where the two coincide (titles, summaries, compaction); quality-judging for main-turn classes is explicitly future work.
- **A5. The dial surface.** A `/v1/dial` endpoint (and scorecard section) reporting per-class volume, local share, adequacy stats (live and shadow), and both counterfactual directions from A4.
- **A6. Earned promotion.** Per-class routing lives in `loxo.toml`; `/v1/dial` produces promotion recommendations once a class accumulates N shadow samples above an adequacy threshold, with the evidence attached; a human edits the config. (Per the non-goals above, promotion is never automatic.) Concrete values for N and the threshold are set in the v0.4 implementation plan, informed by the request volumes the v0.2 ledger actually observes.
- **A7. Evidence supply — field data vs. lab instrument.** The dial needs traffic, and one operator's sessions may not supply enough volume across all classes. Two supply lines, structurally separated:
  - *Real traffic* — the operator's own sessions, other recruited operators (recruitment: issue [#6](https://github.com/brendanlefebvre/loxo-llm-router/issues/6), see risk 8), and agent-driven maintenance chores on the operator's actual repos (dependency bumps, test gaps, docs): genuine work with genuine request shapes. This is the **only** promotion evidence the dial ever reads.
  - *The bench suite* — reproducible headless harness runs (e.g. `opencode run`, scripted Pi) over a fixed task suite, writing to a **separate bench ledger** the promotion logic structurally cannot see. Physical separation, not a source tag with weighting rules — a tag scheme is one weighting bug away from silently poisoning the well. The bench is an instrument for *relative* judgments, where its frozen task distribution is a controlled variable rather than a bias: screening candidate local models before spending shadow samples on them, longitudinal comparison across local model updates, and pre/post regression checks around promotions.

### Track B — Parity (cloud-side capability)

- **B1. Prompt cache injection.** OpenAI-compatible harnesses never send Anthropic `cache_control`, so Loxo enables caching on cloud-bound requests whose target supports it. OpenRouter's chat-completions dialect offers two mechanisms, tried in order of simplicity:
  1. *Automatic:* a top-level `cache_control` field enables auto-advancing cache breakpoints on Anthropic/Vertex/Azure targets — potentially a one-field injection.
  2. *Manual fallback:* per-content-block `cache_control` breakpoints (system prompt, tool definitions, stable conversation prefix), max 4 per request, minimum cacheable prefix 1,024–4,096 tokens depending on model, 5-minute default TTL or 1-hour via `ttl`.

  This is the single largest cost lever for agentic traffic (cache reads cost 0.1x input; writes 1.25–2x). Cache hit rates surface in `/v1/spend`. The v0.2 spike decides between the two mechanisms by measuring real observed hit rates, not docs.
- **B2. Reasoning support.** Tiers gain a reasoning knob mapped to OpenRouter's `reasoning` parameter; reasoning deltas stream through to the client. Verified against what OpenCode and omp actually render.
- **B3. Model metadata honesty.** A virtual tier has no single static cost, so the two metadata consumers are answered differently:
  - *Context length* is well-defined per tier by its routing policy: a tier with cloud escalation effectively has its cloud target's context window (oversized prompts escalate by rule); a hard-pinned local tier has the local context limit. `/v1/models` advertises these, kept truthful against the live rate card rather than hand-maintained.
  - *Price* is advertised as a **ceiling** (the tier's cloud-target rate — the conservative direction: harness estimates can only err high), while per-call truth flows dynamically: `usage.cost` passes through on every cloud response and local-served calls report cost 0. Static metadata says "at most this"; `/v1/spend` says what actually happened. The gap between ceiling and actual is the dial's value, made visible.
- **B4. Parity scorecard.** A living `docs/parity.md` scoring Loxo against the native stack per capability (caching, reasoning, tool fidelity, vision, metadata, streaming, error surfaces, cost) with evidence links. Includes the standing "structurally unclosable" section. The scorecard measures the experience floor, not the project's goal — its job is to prove the floor holds while the dial climbs.
- **B5. Anthropic `/v1/messages` serving endpoint.** Loxo speaks the Anthropic Messages dialect to Claude Code pointed at it via `ANTHROPIC_BASE_URL`; inbound requests translate to the internal OpenAI-compatible form and reuse the existing routing and forwarding unchanged. The hard parts are streaming SSE event translation and tool-call mapping, so B5 gets its own spec plus golden fixtures captured from real Claude Code sessions *before* build — the same harness-as-spec discipline as the other fixtures. API-key-pattern clients only (see non-goals).

## Milestones

- **v0.2 — "Honest cloud."** Feature scope fully merged (A1+A2 observing, B1 cache injection, B3 metadata honesty — plus B2 reasoning, pulled forward from the old v0.3). Remaining to tag: parity scorecard v1 (`docs/parity.md`), golden harness fixtures, and the acceptance check — a real OpenCode session on `loxo/*` populates the adequacy ledger with classified traffic, and shows cache hits in `/v1/spend` and correct context windows. No new features enter v0.2.
- **v0.3 — "Operator-ready."** The recruitment release: everything a potential test operator needs to get the point quickly, see a visible payoff, and start contributing evidence. Contents: the OpenTelemetry trace emitter (observe-only; spec: `docs/specs/otel-trace-emitter.md`), B5 `/v1/messages` translation layer, central ledger collector v1 deployed with opt-in ledger push (invariants in the implementation notes below; transport decided in the collector's own spec), a README/pitch rework leading with visible-payoff artifacts (trace-waterfall screenshot, real `/v1/spend` excerpt, scorecard link), onboarding polish, and omp's first acceptance run.

  **Harness checkpoint #1 — decided 2026-07-20** (taken early at a roadmap checkpoint; it was scheduled for this release because "if the answer is yes, later is more expensive than sooner"):
  - *Claude Code enters scope* (B5). Terms: Anthropic blocks consumer OAuth tokens outside its own products, but the genuine CLI pointed at a custom `ANTHROPIC_BASE_URL` with API-key-style auth is supported, and neither the Commercial Terms nor the AUP prohibit multi-model routing ([analysis](https://autonomee.ai/blog/claude-code-terms-of-service-explained/), [anthropics/claude-code#5577](https://github.com/anthropics/claude-code/issues/5577), [y-router precedent](https://github.com/luohy15/y-router)). Demand: Claude Code remains a top harness by token volume even on open-weight models ([Kimi K3](https://openrouter.ai/moonshotai/kimi-k3), ~226B tokens/30d as of 2026-07-18). The excluded OAuth population is already outside the cost win's audience.
  - *The Pi acceptance slot passes to [Oh-My-Pi](https://github.com/can1357/oh-my-pi)* — omp's richer request shapes (subagents, larger tool inventories) stress the classifier the way real traffic will, while Pi stays deliberately minimal; the acceptance set stays bounded at two harnesses. Pi fixtures are retained informally.

  Acceptance — a stranger-shaped test, following only the README: (a) an operator gets OpenCode routing through Loxo in minutes; (b) they see a per-request trace waterfall in a local Jaeger, including the local→cloud fallback hop as a child span; (c) a real Claude Code session pointed at Loxo via `ANTHROPIC_BASE_URL` completes a working session on `loxo/*` tiers; (d) the suite is green and omp's acceptance session is run and scored; (e) a push-enabled router delivers ledger increments to the deployed collector (verifiably resend-safe), while a down collector or unset push leaves routing and local ledgers entirely unaffected.
- **v0.4 — "Visible dial."** A4 shadow evaluation on chore classes, A5 `/v1/dial`, A7 bench suite v1 (separate ledger from day one), and the first human-flipped chore-class promotion. Acceptance: measurable local share on at least one class, defended by shadow-derived adequacy stats.
- **v0.5 — "Earned share."** A6 promotion recommendations, scorecard v2.
- **Cadence.** Each subsequent minor release repeats the loop: parity gaps found by real sessions, dial share expanded by evidence, harness set reassessed.

## Implementation notes

### Code structure

The router is a single `loxo_llm_router/__init__.py`; this roadmap adds a classifier, a cache injector, an adequacy ledger, shadow evaluation, and a dial endpoint. Rather than grow the monolith, each lands as its own module with one clear seam (following the `config.py` precedent): `classify.py`, `cache.py`, `ledger.py` (absorbing the existing spend accumulator), `dial.py`. The routing core (`pick_target`, `forward`) stays put and stays small.

### State and ledgers

All mutable state lives under one root — `LOXO_STATE_DIR`, defaulting to `~/.local/state/loxo-llm-router/` — because ledgers are operational *state*, not configuration (XDG draws exactly this line; "back up my config" should not drag operational history along, and config can stay read-only). Layout:

```text
$LOXO_STATE_DIR/
  spend.jsonl          # existing spend ledger (relocated)
  adequacy.jsonl       # A2 — the dial's only evidence source
  bench/<run-id>.jsonl # A7 — bench runs, one file per run
```

Rules:

- **Back-compat:** the explicit `SPEND_LEDGER` override keeps working. If the legacy config-dir file exists and the new path doesn't, read the legacy path and log a pointer — never silently fork history into two half-ledgers each claiming to be the total.
- **Docker:** one named volume mounted at the state root makes every present and future ledger durable across rebuilds.
- **JSONL stays the engine** at single-operator scale: append-only files, in-memory aggregates seeded at startup (the existing spend pattern). Revisit trigger for SQLite, written down instead of pre-built: `/v1/dial` seeding or aggregation taking noticeable seconds.
- **Append-only is a design invariant, not an implementation detail:** ledger files are never rewritten in place (rotation is allowed, mutation is not). This is what keeps sync and backup trivial below.
- **A7's separation is structural:** promotion code constructs its reader from the adequacy path alone; the bench writer is a separate module writing per-run files under `bench/`. No shared ledger-router abstraction a bug could cross-wire.
- **State never lives in the working tree** — the repo is public; no gitignore should be the only thing keeping operational data out of the published project.

**Centralized backup and multi-operator pooling — provision now, build later.** When other operators join, their real traffic becomes promotion evidence (A7), so ledgers must eventually flow to a central store; the same mechanism doubles as backup. What gets locked in *now* is the set of invariants that make the future collector cheap, not the collector itself:

- Append-only + per-file byte-offset checkpoints mean incremental push is idempotent — no merges, no conflicts, resend-safe.
- Each router instance *pushes* increments to a token-authenticated central collector (push, not pull: operator machines sit behind NAT). Operator identity is a namespace at the store, never rewritten into entries.
- The central store is additive aggregation and backup only. Local operation never depends on it: a down collector costs sync lag, not routing.
- Ledgers remain metadata-only (see risk 7), so pooling adds minimal sensitivity.

Implementation lands in v0.3 ("Operator-ready"): that release exists to produce the second operator, so the deferral trigger fires just-in-time — collector v1 is deployed and the router gains an opt-in push client (unset/`""` = fully off) ahead of recruitment. The invariants above govern the build; transport, deployment target, and operational cost are decided in the collector's own spec.

### Testing strategy

- **Unit tests** (existing pytest style): classifier taxonomy decisions, cache-breakpoint placement (deterministic given a body), tier metadata synthesis, counterfactual arithmetic.
- **Golden harness fixtures** — the harness-as-spec made executable. Capture sanitized real request bodies (main turns, title/summarize chores, compaction, tool-heavy turns) from OpenCode and omp sessions (and, once B5 lands, Claude Code), pinned per harness version; earlier Pi fixtures are retained as informal regression fixtures with no acceptance-run obligation. Replay them against the app with a stubbed upstream; assert routing decisions, injected fields (`cache_control`, `stream_options.include_usage`), streaming semantics, and error surfaces. When a harness updates and its request shapes drift, the fixtures say so before a user session does.
- **Live smoke + acceptance session.** A scripted end-to-end check against real OpenRouter (cheap model) and a local server, plus — per milestone — one real working session in each target harness. The acceptance session is the only test that can judge "doesn't feel handicapped."
- **The ledgers as longitudinal tests.** The adequacy ledger's `unknown`-class rate is a standing metric of classifier health; a spike means a harness changed shape.
- **CI**: GitHub Actions running unit + fixture tests on every PR (the repo currently has no CI; this roadmap makes it necessary, not optional).

## Risk register ("why it might not be possible" candidates)

1. **Subscription economics.** If the operator's real baseline is a subsidized flat-rate plan (Claude Max class), per-token routing may be structurally unable to match it for heavy usage. *Disposition: the scorecard's cost section answers this with a month of real data — producing that answer is a win condition, not a failure.*
2. **Cache affinity through OpenRouter.** Anthropic cache hits require consecutive requests to land on the same provider. Partially pre-solved: OpenRouter documents "provider sticky routing" after cached requests, deliberately routing follow-ups to the same provider to maximize hits. *Disposition: still spike this first in v0.2 — docs describing sticky routing and a real OpenCode session showing cache-read tokens in `/v1/spend` are different things. If observed hit rates disappoint, the cost leg of the vision needs redesign, and we want to know in week one.*
3. **Classifier fragility.** Heuristic classification breaks when harnesses change their request shapes between versions. *Disposition: observe-only start, `unknown` as the safe default class, per-version golden fixtures.*
4. **Harness-side tuning gap.** Claude Code's prompts are tuned for Claude; OpenCode/omp are model-agnostic. Some experience gap lives in the harness and no router can close it. *Disposition: named and measured in the scorecard rather than silently absorbed as Loxo's failure.*
5. **Shadow load on the working machine.** Shadow evaluation competes for the same GPU/memory as the operator's interactive local traffic. *Disposition: opt-in, per-class sampling rates, and a kill switch; idle-aware scheduling is future work.*
6. **Local server variance.** Local OpenAI-compatible servers differ in usage reporting and tool-call fidelity. *Disposition: token counts fall back to estimation when usage is absent; adequacy signals are computed from the response body, not trusted fields.*
7. **Ledger privacy.** Ledgers record metadata (class, model, counts, outcomes), never message content; shadow evaluation sends content only to the local backend, adding zero new cloud exposure. The future central collector inherits this posture: it aggregates the same metadata-only files, authenticated by token, and holds nothing an operator's local ledger doesn't already hold.
8. **Evidence-supply bootstrapping.** The dial promotes only on real-session evidence (A7), but one operator's sessions may not cover all classes at volume — and the operators who would supply that volume are attracted by a dial that already demonstrably works. *Disposition: the bench suite de-risks model screening without traffic; the pooling invariants (implementation notes above) keep multi-operator support cheap to add; recruitment itself is a distribution problem, not an engineering one — tracked in issue [#6](https://github.com/brendanlefebvre/loxo-llm-router/issues/6), activated after v0.3 — the operator-ready release built to be recruitment's landing page.*

## Open questions (deferred, tracked here so they aren't lost)

- Quality-judging for main-turn classes (LLM-judge comparison of shadow outputs) — required before the dial can ever move main-turn traffic.
- Central ledger collector transport (HTTP push to a small collector service vs. object storage) — decided in the v0.3 collector spec; the append-only invariants above keep both options open.
- Idle-aware shadow scheduling.
- Automatic promotion, revisited only after adequacy metrics have a track record a human has learned to trust.
