# Agent Archetype Tiers — Design Spec

**Date:** 2026-06-24
**Status:** Approved (pre-implementation)
**Related:** `2026-06-22-virtual-model-abstraction-design.md` (this builds directly on the `VirtualModel` registry introduced there)

## Problem

Today the router exposes a single virtual model, `airwolf/auto`, which does
local-first routing and escalates to cloud (`z-ai/glm-5.2`) on size or
`x-quality: best`. That is the right *default*, but it gives no way to express
**task-appropriate model selection**. Concretely: when mechanically executing a
detailed plan, a cheap/fast model is the right tool; when doing architecture or
hard debugging, a top-tier reasoning model is. There is currently one knob for
all work.

The goal is **agent archetypes**: OpenCode agents that each bind to a model
*tier*, so the cost/quality lane follows the kind of work — cheap by default,
expensive only on deliberate request. This matters now because cloud spend is
being kept deliberately low.

## Two surfaces (unchanged philosophy)

Per the virtual-model design, there are two config surfaces and the abstraction
lives in the **router**, not the client:

- **Router** — defines what each tier *means*: real cloud/local target, routing
  policy, vision contract, advertised context. The client never sees raw
  upstream ids.
- **OpenCode** — defines *archetypes* (agents): each agent's `model` points at a
  router tier id (`airwolf-llm-router/<id>`). OpenCode supports per-agent `model`
  in `opencode.json` or markdown files under `~/.config/opencode/agents/`.

## Tier roster

All ids and targets are environment-overridable, matching the existing pattern.
Prices are OpenRouter live rates as of 2026-06-24 ($/Mtok input → output);
context from OpenRouter model metadata.

| Tier id | routing | cloud target | local target | vision | cost in→out | ctx |
|---|---|---|---|---|---|---|
| `airwolf/auto` | `auto` | `z-ai/glm-5.2` | first `LOCAL_MODELS` | `shim` | $0.95 → $3.00 | 1.0M |
| `airwolf/fast` | `cloud` | `z-ai/glm-4.7-flash` | — | `reject` | $0.06 → $0.40 | 203K |
| `airwolf/deep` | `cloud` | `google/gemini-2.5-pro` | — | `native` | $1.25 → $10.00 | 1.0M |
| `airwolf/local` | `local` | — | `Qwen3.6-35B-A3B-4bit` | `local` | free | — |

Notes:
- `airwolf/auto` behavior is **unchanged** — same routing, same cloud target,
  same vision shim. It remains the everyday default.
- `deep` = `gemini-2.5-pro` was chosen over a top GLM (too close to `auto` to be
  a real step-up) and over Opus 4.8 (frontier-class quality at ~¼ the input cost;
  the cost-rational pick). Routed via OpenRouter, like all cloud traffic.

## Router changes (`llm_router.py`)

### 1. `VirtualModel` data model

Two new/changed fields:

```python
@dataclass(frozen=True)
class VirtualModel:
    id: str
    cloud_target: str | None = None      # now optional (local-pinned tiers have none)
    local_target: str | None = None
    routing: str = "auto"                 # "auto" | "cloud" | "local"
    vision: str = "shim"                  # "native" | "shim" | "local" | "reject"
    advertised_context: int = 1_048_576
```

`routing` and `vision` defaults (`"auto"`, `"shim"`) reproduce today's
`airwolf/auto` behavior exactly, so the existing entry is unchanged in effect.

`cloud_target` becomes optional because the local-pinned tier has none. Any code
that iterates `cloud_target` — notably the live rate-card fetch and the
vision/shim self-check — must skip tiers whose `cloud_target` is `None`.

### 2. Registry

The registry holds four entries total. `airwolf/auto` already exists, keeping its
current `AUTO_MODEL_ID` / `AUTO_CLOUD_MODEL` / `AUTO_LOCAL_MODEL` overrides
unchanged. The three **new** tiers add their own env overrides, following the same
style:

- `airwolf/fast`: `FAST_MODEL_ID`, `FAST_CLOUD_MODEL`
- `airwolf/deep`: `DEEP_MODEL_ID`, `DEEP_CLOUD_MODEL`
- `airwolf/local`: `LOCAL_TIER_MODEL_ID`, `LOCAL_TIER_MODEL` (the local target)

The local tier uses the `LOCAL_TIER_` prefix deliberately, to avoid collision
with the existing `LOCAL_BASE_URL` / `LOCAL_MODELS` / `LOCAL_CONTEXT_LIMIT` vars.
`airwolf/local`'s local target falls back to first `LOCAL_MODELS` when unset, via
the existing `local_target_for` helper.

### 3. Routing logic (`pick_target`)

Branch on `vm.routing`:

- **`cloud`** → always `(CLOUD_BASE_URL, vm.cloud_target, "virtual-pinned-cloud")`.
  Ignores prompt size and `x-quality` (the tier is already its own lane).
- **`local`** → always `(LOCAL_BASE_URL, local_target_for(vm, model), "virtual-pinned-local")`.
- **`auto`** → unchanged (quality-best / oversize → `cloud_target`, else local).

### 4. Hard-fail for the local pin

The cloud transport-fallback currently added in `chat_completions` whenever
`base_url == LOCAL_BASE_URL` must be **suppressed when the resolved VM has
`routing == "local"`**. Result: an oversized prompt or an unreachable/timed-out
local server surfaces as a clear error, never silent cloud spend. "Local means
local."

This is the one behavioral subtlety: the fallback decision must be aware of the
resolved tier, not just the chosen base URL.

### 5. Recovery from a local hard-fail

A hard-fail is recovered by a **deliberate tier switch, not an automatic retry** —
that is the point: it converts silent surprise cloud spend into a one-keystroke
choice. In OpenCode the user is in the `local` primary agent; on the 422 they Tab
to `auto` (or `deep`) and re-send. Switching primary agent preserves the session,
so the resend carries conversation context.

| Hard-fail cause | Remedy surfaced to the user |
|---|---|
| Prompt exceeds local context | Tab to `auto` (escalates to cloud) or `deep` (1M ctx) |
| Local server unreachable / timed out | Start the MLX server, or Tab to `auto` |
| Image, thin OCR or no shim | Tab to `deep` (native vision) |

Two requirements make recovery real:

1. **The 422 message names the remedy, not just the cause** — e.g. *"local server
   unreachable; start MLX or switch to airwolf/auto."* Recovery guidance lives in
   the error text.
2. **OpenCode must surface the 422 body.** If the TUI shows only a bare status
   code and swallows the message, the guidance is lost. Validation step: confirm
   OpenCode renders the error body; if it truncates, mirror the recovery
   instruction in the `local` agent's description/prompt so it stays discoverable.

No per-request "escape hatch" header is provided: OpenCode cannot easily set
per-message headers, and it would reintroduce the silent-spend path the local pin
exists to close.

### 6. Vision contract per tier

`apply_vision_policy` (and its caller) honor `vm.vision`:

- **`native`** — target sees images itself; pass through untouched. Add
  `google/gemini-2.5-pro` to `VISION_CAPABLE_MODELS` so the shim is skipped.
- **`shim`** — existing full policy (local OCR, may escalate to cloud if thin),
  governed by `VISION_MODE` / `x-vision`. Unchanged; used by `auto`.
- **`local`** — on-machine OCR only (reuse `apply_vision_shim`); if the OCR is
  thin (`< VISION_OCR_MIN_CHARS`) **or** no `VISION_SHIM_MODEL` is configured,
  **hard-fail with 422**. Never escalates to cloud (would break the local pin).
- **`reject`** — any image content → **hard-fail with 422** before forwarding.

All hard-fails return HTTP 422 with a legible message, e.g.
*"airwolf/fast has no vision; use airwolf/auto or airwolf/deep"*. The reject path
is signaled out of the vision step (e.g. a `VisionRejected` exception caught in
`chat_completions` → `JSONResponse(status_code=422, ...)`); exact mechanism is an
implementation detail.

### 7. `/v1/models` advertisement

`_virtual_model_entries()` already iterates `VIRTUAL_MODELS`, so the three new
tiers are advertised automatically. No change beyond the registry.

## OpenCode changes

### Model entries (`opencode.json`)

Add to `provider.airwolf-llm-router.models` (alongside the existing
`airwolf/auto`):

- `airwolf/fast` — `limit.context: 202752`, text-only modalities.
- `airwolf/deep` — `limit.context: 1048576`, modalities include `image`.
- `airwolf/local` — context per the MLX model, text-only modalities.

### Archetypes (agents) — asymmetric: auto-down / explicit-up

The model-tier decision is left to **you** for the expensive lane, and allowed to
be model-chosen only for the cheap lanes (a misfire there is cheap). This keeps a
hard cost guarantee where the wallet is exposed.

| Archetype | Kind | Tier | Invocation |
|---|---|---|---|
| (default) | primary | `airwolf/auto` | default agent |
| `plan-executor` | **subagent** | `airwolf/fast` | auto-invoked by the primary for mechanical plan execution; also `@plan-executor` |
| `architect` | **primary** | `airwolf/deep` | explicit only — Tab or `@architect`; never auto-escalated |
| `local` | **primary** | `airwolf/local` | explicit "offline / private" switch |

Only `plan-executor` auto-invokes in v1 — one `description` to tune, not three.
A `local` grunt subagent (auto-invoked downward) is a deferred enhancement.

## Open risk — superpowers dispatch coupling

OpenCode subagent auto-invocation is driven by the primary model matching agent
`description`s. superpowers has its **own** dispatch logic and tends to target a
`general-purpose` subagent, so there is **no guarantee** that plan execution will
route through `plan-executor` (and thus `airwolf/fast`).

This is treated as **verify, not assume**:

1. After wiring, run a real superpowers plan execution.
2. Check `GET /v1/spend` `by_model` to confirm `z-ai/glm-4.7-flash` is the model
   actually billing for the execution work.
3. If it is not: fall back to explicit `@plan-executor`, or switch to a `fast`
   **primary** during execution. Document whichever works.

The expensive lane (`deep`) is deliberately *not* dependent on this coupling —
it is explicit-only — so the worst case of a dispatch misfire is "cheap work ran
on the everyday default," never "expensive work ran by surprise."

## Testing

Extend `test_routing.py` (characterization tests, the established pattern):

- `routing: "cloud"` tier → always cloud target, regardless of prompt size /
  `x-quality`.
- `routing: "local"` tier → always local target, **and the cloud fallback is not
  attached** (assert the hard-fail path).
- `routing: "auto"` tier → behavior unchanged (regression guard).
- Vision: image + `vision: "reject"` tier → 422; image + `vision: "local"` with
  thin OCR / no shim → 422.

## Non-goals

- Auto-invoking the expensive `deep` tier (explicit-only by design).
- A `local` auto-invoked grunt subagent (the future "C" add-on: per-agent
  override on top of named tiers).
- Feeding live cost into OpenCode's display (still no wire channel; use
  `/v1/spend`).
- Generating the OpenCode config from the registry (hand-maintained, as before).

## Summary of decisions

- Four named tiers; `auto` unchanged, `fast`/`deep` pinned cloud, `local` pinned
  local with hard-fail.
- `fast` = `glm-4.7-flash`, `deep` = `gemini-2.5-pro`, `auto` keeps `glm-5.2`
  (cost-rational choices; OpenRouter-only).
- Routing and vision become small explicit policy fields on `VirtualModel`.
- OpenCode archetypes are asymmetric: cheap lanes may auto-invoke, the expensive
  lane is explicit-only.
- superpowers dispatch coupling is a validation step, not an assumption.
