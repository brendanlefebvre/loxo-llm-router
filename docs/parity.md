# Parity scorecard — v0.2.0 (2026-07-20)

Loxo vs. the native stacks, per capability, with evidence. This measures the
experience floor (ROADMAP win condition 3), not the project's goal: its job
is to prove the floor holds while the dial climbs.

| Capability | Status | Evidence |
|---|---|---|
| Prompt caching | ✅ Working (provider-dependent) | Spike: docs/spikes/2026-07-cache-affinity.md (~9.7x cached repeats; `auto` chosen). Live sessions: anthropic/claude-sonnet-4.6 98-99% of prompt cached turn-over-turn (~7-8x cheaper); google/gemini-2.5-pro implicit caching, variable (87-93% on hits, misses interleaved); moonshotai/kimi-k2.6 zero cache observed across consecutive 16-17k-token prompts (Pi session 2026-07-20). Merged-main smoke: savings surfaced in /v1/spend. |
| Reasoning | ✅ Working (both harnesses) | Tier knob -> OpenRouter reasoning.effort. OpenCode 2026-07-20: thinking spinner + expandable text. Pi 2026-07-20: traces rendered by default (thinkingFormat openrouter); Pi sends its own `reasoning: {effort: medium}`, which correctly wins over the tier knob (client-wins path verified in captured traffic). Reliability notes: one Kimi mid-stream truncation (~29s, no finish_reason/usage — ledger-fingerprinted) and one Gemini mid-stream error event surfaced by Pi; both transient, retry-recovered. |
| Tool fidelity | ✅ Observed | Adequacy ledger records tool-call JSON validity per request; acceptance session: all tool-call turns valid. Golden fixtures pin the 11-tool OpenCode schema shape. |
| Vision | ✅ Unchanged | v0.1 shim/policy machinery untouched by v0.2. |
| Metadata | ✅ Working | /v1/models: rate-card-derived context windows (loxo/local honestly 60,000), per-token price ceilings; local non-streaming usage.cost 0. Verified on merged main 2026-07-20. |
| Streaming | ✅ Working | Byte-level passthrough; no masked non-200s (tested); reasoning deltas flow. |
| Error surfaces | ✅ Working | Upstream non-200s surface with real status; truncated upstream streams are recorded in the adequacy ledger (finish_reason: None fingerprint) even though they cannot be restarted mid-flight. |
| Cost | 🟡 Collecting | usage.cost passthrough + /v1/spend live; cache savings estimated per model. Win-condition comparison needs a representative month of real metered spend (ROADMAP risk 1) — collection started with v0.2. |

## Classifier health (A1)

Acceptance session 2026-07-20: `chore` and `main` classified on local turns;
62% `unknown` overall — dominated by cloud turns run under OpenCode's
Architect agent, whose system prompt has not been captured. v2 fingerprints
cover OpenCode's build agent (two model-family variants + /compact) and Pi
(main incl. developer-role delivery + compaction, captured 2026-07-20). This
is unknown-default working as designed: unseen prompts are surfaced, not
guessed. TODO: capture one Architect-agent session. Pi shows no LLM title
chore, so `chore` remains OpenCode-only.

## Structurally unclosable (standing section)

Nothing confirmed unclosable yet. Watching:
- Local streaming usage.cost: deliberately not rewritten (SSE mutation);
  harness-side estimates err high off the advertised ceiling. Scoped out by
  design, not proven impossible.
- Subscription economics (ROADMAP risk 1): per-token routing vs. subsidized
  flat-rate plans — answered with data after a month of collection, not
  asserted.

## Known gaps (tracked)

- Exception-path requests write no adequacy entry (mid-stream disconnects,
  transport errors without fallback), and mid-stream SSE `error` events are
  not recorded; schema marker + error-event signal planned pre-dial (v0.4).
- Architect-agent fingerprint uncaptured (see classifier health).
