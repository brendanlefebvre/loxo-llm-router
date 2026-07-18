# Spike: cache affinity through OpenRouter (2026-07)

**Question** (ROADMAP risk 2): do prompt-cache discounts materialize through
OpenRouter's chat-completions dialect, and which injection mechanism should B1
use — top-level auto `cache_control`, or per-block manual breakpoints?

**Method:** `scripts/cache_spike.py` — OpenCode-shaped multi-turn conversations
sent directly to OpenRouter, raw `usage` recorded per call. Models:
`anthropic/claude-haiku-4.5` (all three mechanisms), `anthropic/claude-sonnet-4.6`
(winner confirmation). Note: the script's in-code comment describes the system
prompt as "~6k tokens"; the actual stable prefix (system rules text + 3 tool
schemas) measured **141,552 chars (~35,388 tokens, `len // 4`)** via
`--dry-run`. All cost/ratio interpretation below accounts for the real ~35k
prefix, not the comment's stale estimate.

**Total real spend across all invocations: ~$0.52** (see Commands below —
spread over a full `--mechanism all` Haiku run, a Sonnet confirmation run that
hit a script bug, and a small corrective re-run). No invocation exceeded the
default `--max-usd 1.00` guard.

## Results

All values are the literal `usage` object per call, model
`anthropic/claude-haiku-4.5` unless noted. `cost` is OpenRouter's
`usage.cost` (USD); `cached_tokens` is `usage.prompt_tokens_details.cached_tokens`.

| mechanism | call | prompt_tokens | cached_tokens | cost USD | provider |
|---|---|---|---|---|---|
| none | turn 0 | 34670 | 0 | 0.034990 | Amazon Bedrock |
| none | turn 1 | 34750 | 0 | 0.035070 | Amazon Bedrock |
| none | turn 2 | 34836 | 0 | 0.035091 | Amazon Bedrock |
| none | turn 3 | 34900 | 0 | 0.035045 | Amazon Bedrock |
| none | repeat-final | 34926 | 0 | 0.034941 | Amazon Bedrock |
| auto | turn 0 | 34670 | 0 | 0.043657 | Google |
| auto | turn 1 | 34750 | 34667 | 0.003885 | Google |
| auto | turn 2 | 34832 | 34747 | 0.003875 | Google |
| auto | turn 3 | 34904 | 34829 | 0.003721 | Google |
| auto | repeat-final | 34930 | 34901 | 0.003541 | Google |
| manual | turn 0 | 34670 | 0 | 0.043574 | Google |
| manual | turn 1 | 34750 | 34667 | 0.003886 | Google |
| manual | turn 2 | 34836 | 34747 | 0.003900 | Google |
| manual | turn 3 | 34915 | 34814 | 0.003719 | Google |
| manual | repeat-final | 34935 | 34899 | 0.003550 | Google |
| auto (Sonnet) | turn 0 (cold) | 34671 | 0 | 0.130974 | Anthropic |
| auto (Sonnet) | turn 1 | 34751 | 34668 | 0.011669 | Anthropic |
| auto (Sonnet) | turn 2 | 34837 | 34748 | 0.011326 | Anthropic |
| auto (Sonnet) | turn 3 | 34888 | 34834 | 0.010980 | Anthropic |
| auto (Sonnet) | repeat-final | — | — | — | **HTTP 400** (see Findings §4) |
| auto (Sonnet, corrected re-run\*) | turn 0 | 34671 | 34668 | 0.011369 | Anthropic |
| auto (Sonnet, corrected re-run\*) | turn 1 | 34751 | 34668 | 0.011669 | Anthropic |
| auto (Sonnet, corrected re-run\*) | turn 2 | 34837 | 34748 | 0.011326 | Anthropic |
| auto (Sonnet, corrected re-run\*) | turn 3 | 34888 | 34834 | 0.010980 | Anthropic |
| auto (Sonnet, corrected re-run\*) | **true** repeat (ends-in-user) | 34888 | 34885 | 0.010804 | Anthropic |

\*The original Sonnet `repeat-final` call 400'd due to a script bug (§4). A
small ad hoc script (not committed; reconstructs the same 4 turns via
`cache_spike.build_body`) re-ran the conversation and issued a *correctly
shaped* duplicate of the turn-3 request — i.e. what `repeat-final` was
supposed to send — to get a clean measurement. Its turn 0 already shows
`cached_tokens=34668` because the ephemeral cache from the failed run
minutes earlier was still warm (see Finding 3).

Observed provider(s): **Amazon Bedrock** (Haiku, `none` — no `cache_control`),
**Google** (Haiku, `auto`/`manual` — with `cache_control`), **Anthropic**
(Sonnet, `auto`, both runs). Cache pricing corroborated independently against
OpenRouter's public `/models` rate card (fetched during this spike):
`anthropic/claude-haiku-4.5` → input $1.00/Mtok, cache read $0.10/Mtok, cache
write $1.25/Mtok; `anthropic/claude-sonnet-4.6` → input $3.00/Mtok, cache read
$0.30/Mtok, cache write $3.75/Mtok. These are exactly the 10x
read-discount / 1.25x write-premium Anthropic documents, and the observed
per-call costs above match them almost exactly (e.g. Sonnet cold turn 0:
34671 × $3.75/Mtok = $0.130, observed $0.130974).

## Findings

1. **Yes, cached_tokens appeared**, starting at turn 1 (the second call) of
   every `auto`/`manual` conversation, on both Haiku and Sonnet. It rides in
   `usage.prompt_tokens_details.cached_tokens` in the raw OpenRouter response
   — a field the script already extracts. The `none` control showed
   `cached_tokens: 0` on all 5/5 calls, as expected — cache discounts do not
   happen by accident; they require an explicit `cache_control` signal.

2. **Discount ratio, repeat-final (or true repeat) vs `none` control:**
   Haiku: `auto` $0.003541 vs `none` $0.034941 → **9.87x cheaper (89.9%
   discount)**; `manual` $0.003550 vs `none` → **9.84x (89.8% discount)**.
   Sonnet (no `none` control run for Sonnet; comparing the genuine cold-cache
   turn 0 to the corrected true-repeat call): $0.130974 → $0.010804 →
   **12.1x cheaper (91.75% discount)**. Both ratios track the rate-card's
   10x cache-read discount closely; the small deviations are explained by
   completion-token cost being a fixed, non-discounted component of the
   Sonnet comparison (which used a cache-write call as the baseline, not a
   true no-cache-control control).

3. **Sticky routing:** yes, within every mechanism/model run, every call
   (turn 0 through repeat-final) landed on the identical upstream provider —
   5/5 for each of Haiku's three mechanisms, 9/9 for Sonnet's `auto` across
   *two separate script processes run minutes apart*. Notably, presence of
   `cache_control` changed *which* provider Haiku requests routed to (Amazon
   Bedrock without it, Google with it) — this is provider selection coupled
   to feature support, not just session affinity. Separately, the corrected
   Sonnet re-run's turn 0 came back already cached (`cached_tokens=34668`)
   even though it was a brand-new process invocation — the ephemeral cache
   from the earlier failed run (minutes prior) was still warm, confirming
   the cache key survives across process/connection boundaries, consistent
   with Anthropic's ~5-minute ephemeral TTL.

4. **Errors/rejections:** `none`/`auto`/`manual` on Haiku: zero errors across
   15 calls. `auto` on Sonnet: the `repeat-final` call returned **HTTP 400**:
   `"This model does not support assistant message prefill. The conversation
   must end with a user message."` Root cause is a bug in
   `scripts/cache_spike.py`'s `run_mechanism()`: the assistant reply is
   appended to `messages` immediately after every `"turn"` call (including
   turn 3), so by the time `repeat-final` builds its body, the conversation
   already ends in an assistant message instead of duplicating turn 3's
   user-ending request. OpenRouter's error metadata shows it retried across
   4 upstream routes (Anthropic, Google Vertex, Anthropic again, then Google
   which itself 429'd) before surfacing this 400 from an Azure route — all
   of them rejected the same malformed shape. Claude Haiku 4.5's providers,
   by contrast, silently *accepted* the same malformed (assistant-ending)
   shape as an assistant-prefill continuation and returned 200 with valid
   cache data — so Haiku's `repeat-final` numbers above measure a prefill
   continuation, not a pure duplicate re-read, though the cache-hit
   evidence they contain is still valid. This is a **script defect**, not a
   caching failure — a corrected re-implementation of the same request
   (ending in the user message, exactly duplicating turn 3) confirmed cache
   behavior holds cleanly on Sonnet (see the "corrected re-run" rows above).
   `scripts/cache_spike.py` should be fixed (defer the assistant-append past
   the repeat-final build, or build repeat-final from the pre-append
   messages list) before it is reused for a future spike.

## Decision

B1 uses **auto** because `manual` produced statistically indistinguishable
cache behavior (same cached-token ratios, same costs, same discount) on
Haiku, while carrying extra complexity `auto` doesn't: per-block breakpoint
placement logic and management of OpenRouter's 4-breakpoint cap as
conversations grow turn over turn. `auto` is a single top-level
`cache_control: {"type": "ephemeral"}` flag that OpenRouter applies to
supported upstream targets automatically, is simpler for B1 to implement and
maintain, and was confirmed to produce equivalent cache discounts on both the
cheap probe model (Haiku) and the router's actual Sonnet default target.

## Implications for B1 (PR 5)

- Eligibility check: rate-card `cache_read_per_mtok` non-null — **confirmed
  sufficient**. The router already parses this from OpenRouter's `/models`
  payload (`loxo_llm_router/__init__.py::_parse_rate_card`, tested in
  `test_routing.py`). Both `anthropic/claude-haiku-4.5` ($0.10/Mtok cache
  read vs $1.00/Mtok input) and `anthropic/claude-sonnet-4.6` ($0.30/Mtok vs
  $3.00/Mtok) report non-null cache-read pricing at a 10x discount, matching
  what the spike measured empirically — B1 can gate on
  `rate_card["cache_read_per_mtok"] is not None` with no new field needed.
- No breakpoint placement work needed since `manual` wasn't selected; if it's
  ever revisited, the spike's 2-breakpoint layout (system text block + most
  recent assistant message) worked cleanly and stayed under the 4-breakpoint
  cap through 4 turns.
- TTL: cache survived across separate process invocations several minutes
  apart, consistent with the documented ~5-minute ephemeral TTL — B1 should
  not assume cache warmth requires a persistent connection or single
  process, but also should not assume warmth beyond that window.
- Provider coupling: adding `cache_control` changed which upstream provider
  Haiku requests landed on (Bedrock → Google) in this spike. B1 should watch
  cost/latency after enabling `auto`, since the provider OpenRouter routes
  to with caching enabled may differ from today's default routing, and that
  could have its own latency/availability characteristics.
- Known script defect (see Finding 4): `scripts/cache_spike.py`'s
  `repeat-final` call is malformed for all mechanisms today, though it only
  surfaced as a hard failure on Sonnet. Fix before any future reuse of this
  script; it doesn't block B1 since B1 will construct real request bodies,
  not reuse the spike script.
