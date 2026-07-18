# Design: Positioning & Honesty Docs Revision (issues #4, #5, #6)

**Date:** 2026-07-18
**Status:** Draft for review
**Scope:** README.md + ROADMAP.md only. Docs-only PR, landed before v0.2 planning resumes.
**Spec lifecycle:** tracked on this branch only (`git rm` before merge, per convention).

## 1. Context

Three open issues, all filed 2026-07-17, cluster into one revision:

- **#4 — name the target operator.** The cost pitch is aimed at no one; a skeptical Claude Max subscriber does the math and correctly bounces. Risk 1 already knows this but hides it in a footnote.
- **#5 — near-term savings are chore-class savings.** `loxo/auto` escalates big prompts to cloud, so main-turn spend stays cloud until main-turn quality-judging exists (deferred). Win condition 2 must not be read as an immediate big-ticket outcome.
- **#6 — operator recruitment (placeholder).** Promotion needs real-session volume; volume needs a working dial. Chicken-and-egg, explicitly post-v0.2. Needs acknowledgment, not design.

#4 and #5 are siblings — together they make the cost claim honest on *who* and *when*. #6 is different in kind (distribution, not engineering) and gets the lightest honest touch.

## 2. Decisions (settled in brainstorming, 2026-07-18)

1. **Win condition 2 is reworded**, not annotated around: its baseline becomes the operator's actual **metered (pay-per-token) spend**, with the subsidized-subscription case named out of scope for the cost win. Positioning and win condition then agree; risk 1 keeps its "measure it anyway" disposition.
2. **#6 lands as a risk-register entry plus an A7 cross-reference.** The chicken-and-egg is a genuine "why it might not be possible" candidate. No milestone slot, no committed design; issue #6 stays open.
3. **Standalone docs PR, before v0.2 planning** — same pattern as the track swap: planning should read from a roadmap that already says the honest thing.

## 3. README changes

### 3a. New section: "Who this is for" (issue #4)

Placed after the roadmap blockquote, before Quickstart — the reader decides whether to invest before being shown install commands. Proposed text:

> ## Who this is for
>
> loxo's cost story is real for some operators and honestly not for others:
>
> - **Pay-per-token users** — you meter API spend today (OpenRouter, direct API
>   keys). Routing chore traffic local and tracking every cloud dollar attacks
>   the bill you actually pay.
> - **Privacy / local-first users** — you want work kept on-device wherever
>   adequacy allows, at any price.
> - **Multi-model routers** — you already mix models per task and want that
>   selection measured instead of vibes-based.
>
> **Not a cost win:** heavy users on subsidized flat-rate subscriptions
> (Claude Max class). Per-token routing cannot beat a subsidized subscription
> by arithmetic — if that's your baseline, loxo earns its keep only on the
> privacy and measurement axes, not price.
>
> Near-term savings concentrate in the **chore traffic** (titles, summaries,
> compaction); shifting main-turn work local is the hard, later win — see the
> [ROADMAP](ROADMAP.md).

The final paragraph is #5's README half — one sentence, woven here rather than a separate section.

### 3b. No other README changes

The intro, quickstart, tiers, configure, and learn-more sections are untouched.

## 4. ROADMAP changes

### 4a. Win condition 2 reworded (issue #4, decision 1)

Current text:

> 2. **Cost** — `/v1/spend` for a representative month is at or below the operator's actual current monthly spend. Parity is a win; meaningfully more expensive for a similar experience is *the* loss condition.

Proposed replacement:

> 2. **Cost** — `/v1/spend` for a representative month is at or below what the operator actually pays today on **metered, per-token pricing**. The baseline is deliberately metered spend: against a subsidized flat-rate subscription (Claude Max class) under heavy use, per-token routing cannot win by arithmetic, so that case is out of scope for the cost win and said plainly in the README's "Who this is for" (risk 1 still measures it with real data). Parity is a win; meaningfully more expensive for a similar experience is *the* loss condition.

### 4b. Sequencing note after the win-conditions list (issue #5)

New short paragraph immediately after the numbered list:

> **Sequencing honesty:** near-term movement on conditions 1–2 concentrates in the chore classes (titles, summaries, compaction) — exactly the classes where mechanical adequacy signals suffice (A4). Main-turn traffic, where spend actually concentrates, stays cloud until main-turn quality-judging exists (an explicitly deferred open question). First sessions should expect chore-class savings, not big-ticket ones.

### 4c. Risk register: new entry 8 (issue #6)

> 8. **Evidence-supply bootstrapping.** The dial promotes only on real-session evidence (A7), but one operator's sessions may not cover all classes at volume — and the operators who would supply that volume are attracted by a dial that already demonstrably works. *Disposition: the bench suite de-risks model screening without traffic; the pooling invariants (implementation notes above) keep multi-operator support cheap to add; recruitment itself is a distribution problem, not an engineering one — tracked in issue [#6](https://github.com/brendanlefebvre/loxo-llm-router/issues/6), targeted after v0.2 ships an adequacy ledger worth pooling.*

### 4d. A7 cross-reference (issue #6)

In A7's *real traffic* bullet, "other recruited operators" gains a pointer: `other recruited operators (recruitment: issue #6, see risk 8)`.

### 4e. Risk 1 stays put

Its wording already matches the new win condition 2 ("subsidized flat-rate plan… structurally unable to match"); no change needed beyond the WC2 text now pointing at it.

## 5. Out of scope

- **ARCHITECTURE.md** — untouched; it documents shipped reality and none of this is behavior.
- **Recruitment design** (#6's option list — public push, low-friction opt-in, incentives) — stays in the issue; the roadmap only names the risk.
- **Any code or config.**

## 6. Delivery

- Branch `worktree-docs+positioning-honesty` → PR to main, docs-only.
- PR body: `Closes #4`, `Closes #5`, `Refs #6` (#6 stays open as the tracking placeholder).
- This spec file is `git rm`'d before merge (branch-history-only, per convention).
- After merge: update the `roadmap-dial-north-star` memory (its win-condition-2 line still says "at/below actual monthly spend" without the metered qualifier).
