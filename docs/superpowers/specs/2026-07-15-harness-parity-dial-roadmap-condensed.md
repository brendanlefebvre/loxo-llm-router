# Loxo Roadmap — Condensed

Companion to `2026-07-15-harness-parity-dial-roadmap-design.md` (the full
spec); this version trades detail for a five-minute read.

**The aspiration — not a description; none of this is true today.** Loxo
becomes the dial that tracks the local/frontier boundary as it moves. You
point OpenCode or Pi at Loxo with an OpenRouter key (and optionally a local
model server); local models absorb an evidence-earned, growing share of a
real agentic workload; monthly cost lands at or below what the operator
actually spends today; and the session stays good enough that choosing Loxo
over the native stacks never feels like a sacrifice.

**Where Loxo stands (v0.1.0):** an OpenAI-compatible proxy with heuristic
tier routing, local→cloud fallback, a vision policy, a spend ledger, and a
live rate card. No classification, no adequacy measurement, no cache
injection, no dial. The distance between these two paragraphs is the roadmap.

**Win conditions, in priority order:**

1. **Local absorption** — the headline metric: the fraction of real-session
   tokens served locally *with proven adequacy*, growing release over release.
2. **Cost** — a representative month at or below actual current spend.
   Meaningfully more expensive for a similar experience is the loss condition.
3. **Experience floor** — real sessions must stay above "I'd still choose
   this for real work today." A dial with no traffic measures nothing.

**Two tracks, every release advancing both.**

- *Dial (headline):* classify each request by observable shape
  (main / chore / compaction); record per-class adequacy signals in a ledger;
  **shadow-evaluate** local models on sampled copies of cloud-bound traffic,
  off the critical path; promote a class to local only when the evidence
  supports it — and only by human decision, never automatically, because a
  wrong promotion is an invisible quality regression.
- *Parity (floor):* inject Anthropic `cache_control` breakpoints that
  OpenAI-compatible harnesses can never send themselves (~10x cheaper input
  tokens); reasoning passthrough; honest tier metadata (context defined by
  routing policy, price as a ceiling plus per-call truth via `usage.cost`);
  all scored against the native stacks in a living parity scorecard.

**Milestones.** v0.2 "Honest cloud" — observation ships, caching lands.
v0.3 "Visible dial" — `/v1/dial`, shadow evaluation, first promoted class,
and the first harness checkpoint: reassess the target-harness set and decide
whether Claude Code enters scope via an Anthropic `/v1/messages` endpoint.
v0.4 "Earned share" — promotion recommendations, second-harness acceptance
run, and (if green-lit) the `/v1/messages` translation layer.

**Honesty mechanism.** The scorecard keeps a standing list of gaps that may
be structurally unclosable (subscription economics, harness-side model
tuning). "Here is exactly why it's not possible" is a first-class outcome of
this roadmap, not a failure state.
