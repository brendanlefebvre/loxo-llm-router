# Loxo Roadmap: Harness Parity and the Local/Frontier Dial

**Status:** Reviewed 2026-07-15; execution proceeds via per-milestone plans (v0.2 first)

## North Star & Win Conditions
Loxo becomes the dial tracking the local/frontier boundary. You point OpenCode/Pi at Loxo with an OpenRouter key; session feels foundation-quality from turn one; cost ≤ current spend; local models absorb growing traffic share invisibly.

**Win conditions (priority):** (1) Capability parity with native stacks via evidence-backed scorecard, (2) Cost at/below operator's actual monthly spend, (3) Per-request-class local share measured, visible, and expanding with evidence.

**Explicit non-goals:** Claude Code support, arbitrary harness spec-completeness, automatic unsupervised promotion.

## Roadmap: Two Tracks, Three Milestones

### Track A — Parity (cloud capability)
- **A1 Cache injection:** Inject Anthropic `cache_control` breakpoints on cloud requests (system prompt, tools, conversation prefix) — single largest cost lever
- **A2 Reasoning support:** Tier knob mapped to OpenRouter `reasoning` parameter, streaming to client
- **A3 Metadata honesty:** Context length per routing policy; price advertised as ceiling (cloud rate), actuals via `usage.cost`
- **A4 Parity scorecard:** Living `docs/parity.md` with "structurally unclosable" section

### Track B — The Dial (measurement before movement)
- **B1 Classification:** Heuristic taxonomy from observable shape (`main`, `chore`, `compaction`, `unknown`)
- **B2 Adequacy ledger:** JSONL twin of spend ledger with class, route, model, outcome signals
- **B3 Observe-only first:** Ship B1+B2 changing no routing
- **B4 Counterfactual economics:** Local→cloud = token math; cloud→local via shadow evaluation (sampled, fire-and-forget, off-path)
- **B5 Dial surface:** `/v1/dial` endpoint with per-class volume, local share, adequacy stats
- **B6 Earned promotion:** Human-flipped config changes once class hits N shadow samples above threshold

### Milestones
- **v0.2 "Honest cloud":** A1, A3, scorecard v1, B1+B2 observing
- **v0.3 "Visible dial":** A2, B4 shadow on chore classes, B5 `/v1/dial`, first human promotion
- **v0.4 "Earned share":** B6 recommendations, Pi acceptance, scorecard v2, harness checkpoint #1

## Evidence, Testing, Risks
**Code structure:** New modules (`classify.py`, `cache.py`, `ledger.py`, `dial.py`) with clean seams; routing core stays small.

**Testing:** Unit tests, golden harness fixtures (replay sanitized real requests), live smoke + acceptance sessions, ledgers as longitudinal tests, CI required.

**Key risks:** Subscription economics mismatch, cache affinity through OpenRouter, classifier fragility, harness-side tuning gap, shadow GPU contention, local server variance, ledger privacy (metadata only).