# Virtual Model Abstraction — Design

**Date:** 2026-06-22
**Status:** Approved (design); ready for implementation plan
**File touched:** `llm_router.py` (single-file router), plus the client-side
`~/.config/opencode/opencode-openrouter-and-local.json` and `AGENTS.md`.

## Problem

The router's behavior is correct, but the *configuration* leaks router internals
into the OpenCode (client) config in ways that mislead a future reader. On the
`z-ai/glm-5.2` model entry in `opencode.json`:

- **A capability lie:** `"attachment": true` + `"modalities": {"input": ["text","image"]}`.
  GLM-5.2 is genuinely text-only (OpenRouter reports `input_modalities: ["text"]`).
  The router's vision shim is what actually handles images; OpenCode only allows
  image attachment if its own static config claims the model can see, so the
  config had to lie about a real model.
- **A stale, provider-specific cost hardcode:** `"cost": {"input": 1.0, "output": 4.0}`.
  Copied by hand from one OpenRouter provider's GLM-5.2 price. Goes stale; means
  nothing to a new reader.
- **A leaked upstream id:** the client speaks `z-ai/glm-5.2` directly, coupling
  the client config to the specific cloud model the router happens to use.

Root cause: there are two config surfaces — OpenCode's client config (decides
what the UI allows: attach images? show cost?) and the router (does the real
work: routing, vision shim, spend tracking) — and today the client surface has
to misrepresent a real model to unlock pipeline behavior the router provides.

## Goal

Introduce a **virtual model** owned by the router: a single client-facing id
(`airwolf/auto`) that the router resolves to real upstream models and behaviors.
The abstraction lives **in the router code**. The OpenCode config collapses to
one *honest* entry whose declared capabilities are true statements about the
*pipeline*, not lies about a specific model. Cost stops being a hardcode: the
router fetches a **live rate card** from the provider and exposes it on its own
endpoints, while actual charged cost continues to come from `usage.cost`.

## Key established facts (verified during design)

- **OpenCode reads custom-provider model metadata (cost, capabilities, limits)
  only from static config** — not from the provider's `/v1/models` wire response.
  (OpenCode docs; feature request anomalyco/opencode#9311 tracks a future
  `use_models_dev` opt-in.) Therefore a provider-queried cost **cannot** reach
  OpenCode's spend display; it can only be surfaced router-side.
- **`z-ai/glm-5.2`** via `GET https://openrouter.ai/api/v1/models` (no API key
  for the list): `context_length: 1048576`, `input_modalities: ["text"]`,
  `pricing: {prompt: "0.000001", completion: "0.000004", input_cache_read: "0.00000018"}`
  — i.e. $1 / $4 per Mtok, exactly the value that had been hand-copied.
- The router already records **actual** charged cost via `usage.cost` on cloud
  responses, exposed at `/v1/spend`. The rate card is additive (price *before*
  spending), not a replacement for actuals.

## Decisions (locked)

| Decision | Choice |
| --- | --- |
| Where the abstraction lives | **Inline Python registry** in `llm_router.py` (approach ①), env-overridable ids |
| How many virtual models | **One** smart `airwolf/auto` (engages the full existing heuristic stack) |
| Cost handling | **Drop the OpenCode hardcode**; router exposes a **live rate card**; `usage.cost` actuals unchanged |
| OpenCode config | Hand-maintained, **not generated**; collapses to one honest `airwolf/auto` entry |

## Design

### 1. Data model & registry

A single new structure near the top of `llm_router.py`:

```python
@dataclass(frozen=True)
class VirtualModel:
    id: str                          # client-facing, e.g. "airwolf/auto"
    cloud_target: str                # real upstream id for cloud, e.g. "z-ai/glm-5.2"
    local_target: str | None = None  # real upstream id for local; None = first LOCAL_MODELS entry
    vision: bool = True              # pipeline can accept images (via shim/reroute)
    advertised_context: int = 1_048_576   # informational; surfaced in /health and /v1/models

VIRTUAL_MODELS: dict[str, VirtualModel] = {
    vm.id: vm for vm in (
        VirtualModel(
            id=os.environ.get("AUTO_MODEL_ID", "airwolf/auto"),
            cloud_target=os.environ.get("AUTO_CLOUD_MODEL", "z-ai/glm-5.2"),
            local_target=os.environ.get("AUTO_LOCAL_MODEL") or None,
        ),
    )
}

def resolve_virtual(model_id: str) -> VirtualModel | None:
    return VIRTUAL_MODELS.get(model_id)
```

Each field is consumed by exactly one concern: `cloud_target`/`local_target` by
routing, `vision` by the vision policy, `advertised_context` by the metadata
endpoints. Vision *mechanics* (shim model, OCR thresholds, cloud vision model)
remain in the existing `VISION_*` env vars; the virtual model only flips the
*intent* bit (`vision`), avoiding duplication of that subsystem. Deployment-
specific ids are env-overridable; the structure and intent are legible in code.

### 2. Routing resolution

`pick_target` gains one branch **at the very top**, before the existing rules.
The existing rules (1–5) are unchanged, so raw ids (`z-ai/glm-5.2`,
`mlx-community/...`) keep working exactly as today (backward compatible).

```
0. If model ∈ VIRTUAL_MODELS:                       ← NEW, highest priority
     vm = VIRTUAL_MODELS[model]
     - x-quality: best          → CLOUD, vm.cloud_target
     - prompt > LOCAL_CONTEXT_LIMIT → CLOUD, vm.cloud_target
     - else                     → LOCAL, local_target_for(vm)
   (vision policy then layers on as today, gated by vm.vision)
1.–5. existing rules, unchanged
```

Why branch 0 must be first: `airwolf/auto` contains a `/`, so existing rule 4
("`/` ⇒ provider-prefixed cloud id") would otherwise forward the literal virtual
id upstream. Branch 0 intercepts and substitutes the real upstream id, so no
upstream ever sees `airwolf/auto`.

**Local target resolution** (`local_target_for(vm)`):
- `vm.local_target` if set; else
- the first configured `LOCAL_MODELS` entry (e.g. `mlx-community/Qwen3.6-35B-A3B-4bit`); else
- if `LOCAL_MODELS` is empty, pass the client's original `model` through unchanged
  (degrade, don't crash).

**Vision intent:** `vm.vision` is threaded into `apply_vision_policy` so the
policy is gated by the virtual model's declared capability rather than purely by
global env. For non-virtual ids, vision behavior is exactly as today.

**Transport fallback:** when a *virtual* request routes local and the local
endpoint refuses/times out (`LOCAL_CONNECT_TIMEOUT`), the failover targets
`vm.cloud_target` (not the global `CLOUD_DEFAULT_MODEL`) — "local died" keeps the
caller on the model family they asked for. Non-virtual requests still fall back
to `CLOUD_DEFAULT_MODEL` as today.

### 3. Rate-card lifecycle

The router learns the real price of each virtual model's `cloud_target` from
OpenRouter and exposes it read-only. Purely informational — no effect on routing
or on any request's behavior.

**Source & shape.** `GET https://openrouter.ai/api/v1/models` (no API key). For
each distinct `cloud_target` in the registry, extract:

```python
{
  "model": "z-ai/glm-5.2",
  "input_per_mtok": 1.0,         # pricing.prompt          * 1e6
  "output_per_mtok": 4.0,        # pricing.completion       * 1e6
  "cache_read_per_mtok": 0.18,   # pricing.input_cache_read * 1e6 (if present)
  "context_length": 1048576,
  "input_modalities": ["text"],
  "fetched_at": "<iso8601>",
}
```

**Lifecycle — lazy with TTL, never blocking:**
- In-memory `_rate_cards: dict[str, dict]` + timestamp, guarded by an
  `asyncio.Lock` (mirrors the existing `_spend_lock` pattern).
- Populated on **first access**; refreshed when older than `RATE_CARD_TTL`
  (default 86400 s). **Not** fetched synchronously at startup.
- Single `httpx.AsyncClient` GET, ~10 s timeout. On any failure the rate card
  stays `null`/stale; **no request is ever blocked or failed because pricing is
  unavailable.**

**Surfaced in two existing endpoints:**
- `/health` → add a `rate_cards` block alongside the current `spend` summary.
- `/v1/spend` → add `rate_cards` so watchers see *rate* next to *actuals*.

**Self-check bonus.** Because the fetch returns `input_modalities`, at fetch time
the router logs a one-line sanity check: if a virtual model has `vision: True`
but its `cloud_target` reports `["text"]` (GLM-5.2's case), log
`vision shim required for cloud_target z-ai/glm-5.2 (text-only)`. The assumption
that was formerly a hidden config lie is now asserted in code.

**New env vars:** `RATE_CARD_TTL` (default `86400`), `RATE_CARD_URL` (default the
OpenRouter models URL; overridable for a different cloud base).

### 4. OpenCode config + `/v1/models` advertisement

**OpenCode config** collapses to one honest entry:

```jsonc
// BEFORE — a lie + a hardcode + a leaked id
"z-ai/glm-5.2": {
  "name": "GLM-5.2 (via router)",
  "limit": { "context": 1048576, "output": 32768 },
  "cost": { "input": 1.0, "output": 4.0 },        // stale hardcode, one provider
  "attachment": true,                              // lie about GLM-5.2
  "modalities": { "input": ["text","image"], "output": ["text"] }
}

// AFTER — honest about the pipeline
"airwolf/auto": {
  "name": "Airwolf Auto (local→cloud, vision)",
  "limit": { "context": 1048576, "output": 32768 },
  "attachment": true,                              // true of the pipeline (router shims/reroutes)
  "modalities": { "input": ["text","image"], "output": ["text"] }
  // no cost: real spend at /v1/spend; rate card at /health
}
```

`attachment`/`modalities`/`limit`/`name` must stay (OpenCode reads metadata only
from static config) but each is now a true statement about `airwolf/auto` as a
pipeline. The leaked upstream id and the cost hardcode are gone. The file remains
hand-maintained (not generated) but is now trivially legible. The live
`opencode.json` is a symlink to `opencode-openrouter-and-local.json`; the local
Qwen entry (`mlx-community/Qwen3.6-35B-A3B-4bit`) stays for direct local access.

**`/v1/models` advertisement.** The endpoint (currently merges local + cloud
lists) **prepends** one synthesized entry per registry model so other
OpenAI-compatible clients discover it:

```python
{"id": "airwolf/auto", "object": "model", "owned_by": "airwolf-llm-router",
 "context_length": vm.advertised_context}
```

Advertisement only — it does not change OpenCode's behavior (static config), but
keeps the registry as the single source of truth for what virtual models exist.

## Error handling & degradation

- **Rate-card fetch failure** → rate card `null`/stale; requests unaffected.
- **`LOCAL_MODELS` empty** → local target falls back to the client's original id.
- **Virtual id not in registry** → falls through to existing rules 1–5 (treated
  as a raw id), preserving today's behavior.
- **Vision** subsystem behavior is unchanged except for the added `vm.vision`
  gate; all existing graceful-degradation paths (transcription failure leaves the
  image untouched, etc.) are retained.

## Backward compatibility

Branch 0 triggers only on registered virtual ids. Every existing path — explicit
local ids, provider-prefixed cloud ids, `x-quality`, size gating, transport
fallback for non-virtual requests — is untouched. The change is purely additive.

## Verification (manual; repo has no automated tests)

The project currently has no test harness. Proposed manual verification, plus an
open question on whether to add automated tests (see below):

1. **Routing — virtual id, small prompt:** POST `model: "airwolf/auto"` with a
   short prompt → log shows `-> LOCAL model=mlx-community/Qwen3.6-35B-A3B-4bit`.
2. **Routing — virtual id, large prompt / `x-quality: best`:** → log shows
   `-> CLOUD model=z-ai/glm-5.2`.
3. **No leaked virtual id upstream:** confirm the upstream never receives
   `airwolf/auto` (substitution happened) — check log line `model=` value.
4. **Vision:** attach an image to `airwolf/auto` → vision policy fires per
   `VISION_MODE` (OCR locally or reroute), governed by `vm.vision`.
5. **Rate card:** `curl localhost:9090/health` and `/v1/spend` → `rate_cards`
   block present with live `input_per_mtok`/`output_per_mtok` for `z-ai/glm-5.2`;
   kill network and confirm endpoints still respond (rate card null/stale).
6. **Self-check log:** on first rate-card fetch, confirm the text-only sanity
   line for `z-ai/glm-5.2`.
7. **Backward compat:** direct `z-ai/glm-5.2` and `mlx-community/...` requests
   route exactly as before.
8. **`/v1/models`:** `curl localhost:9090/v1/models` → `airwolf/auto` present and
   prepended.

**Open question (testability):** the routing-decision logic (`pick_target` +
virtual resolution + local-target fallback) is now branchy enough that a small
pure-function unit test would be cheap and high-value (no network: feed a body +
headers, assert `(base_url, model_to_send, reason)`). Decide during planning
whether to introduce a minimal `pytest` for the pure routing functions, or remain
manual-only per current repo convention.

## Out of scope (YAGNI)

- Multiple virtual models / named-intent registry (e.g. `airwolf/best`,
  `airwolf/local`) — explicitly deferred; one `auto` model now.
- Generating the OpenCode config from the registry — deferred; hand-maintained.
- Feeding live cost into OpenCode's display (no wire channel exists today).
- Per-provider endpoint pricing (`/models/:author/:slug/endpoints`) — the
  top-level `/models` price is representative and sufficient for a rate card.
