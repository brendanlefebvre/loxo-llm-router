# Architecture

This document describes what is implemented today. It is updated as part of
any change that alters behavior described here — a PR isn't done until this
file agrees with the code. Design intent for future work lives in
[ROADMAP.md](ROADMAP.md) and is deliberately kept out of this file, so
nothing below describes machinery you can't actually run.

## What Loxo is

A single-process, OpenAI-compatible HTTP proxy that dispatches each request
to a **local** OpenAI-compatible server or to **cloud** (OpenRouter), based
on *intent-expressing heuristics* — the client's request says what it needs,
and the router resolves it — not retry-on-failure.

```text
clients ──▶ loxo-llm-router (:9090/v1) ──┬──▶ local model server (:7979/v1)
                                         └──▶ OpenRouter (cloud)
```

Two modules:

| Module | Role |
|---|---|
| `loxo_llm_router/__init__.py` | FastAPI app: routing, forwarding, vision policy, spend tracking, endpoints |
| `loxo_llm_router/config.py` | Config loading/merging into a frozen `Config`; tier registry (`VirtualModel`) |

## Design principles

- **Intent over retry.** Routing decisions are made up front from what the
  request expresses (model id, headers, size). The only reactive behavior is
  a transport-failure fallback, and it is deliberately narrow (below).
- **No masked errors.** Upstream failures surface with their real status.
  The streaming path connects and checks the upstream status *before*
  committing to a `StreamingResponse`, so a non-200 (unfunded account, 429,
  provider 5xx) is returned as that status — never smuggled inside an HTTP
  200 SSE stream. Fallbacks that would hide a real failure are suppressed
  (see the `local` tier).
- **Secrets are env-only.** `OPENROUTER_API_KEY` and `ROUTER_TOKEN` are read
  from the environment, never from config files.
- **Costs are observed, not estimated.** Spend is recorded from the
  provider's own `usage.cost` field on each response, not reconstructed
  from token math.

## Configuration

Three layers, lowest to highest precedence:

1. **Bundled defaults** (`loxo.default.toml`) — runs out of the box
2. **User TOML** — `$LOXO_CONFIG`, else `./loxo.toml`, else
   `~/.config/loxo-llm-router/loxo.toml` (first hit wins)
3. **Environment** — overrides scalars; the only place secrets may live

Merge semantics are three-level: top-level scalars replace; section tables
merge per key; `[tiers.*]` tables merge **per field**, so a partial
`[tiers.auto]` override changes only the fields it names and inherits the
rest from the bundled tier.

Host/port resolve `LOXO_HOST`/`LOXO_PORT` → bare `HOST`/`PORT` (12-factor
PaaS convention) → `[server]` in TOML → `0.0.0.0:9090`.

## Virtual tiers

Each `[tiers.<key>]` table becomes the client-facing model id
`<namespace>/<key>` (default namespace `loxo`, rebrandable via `namespace`
or `ROUTER_NS`). A tier declares:

- `routing`: `auto` (local unless escalation triggers), `cloud` (pinned),
  or `local` (pinned, hard-fail — never silently spends cloud money)
- `cloud_target` / `local_target`: the real model ids to send
- `vision`: `native` | `shim` | `local` | `reject` (below)
- `advertised_context`: surfaced as `context_length` in `/v1/models`;
  defaults to the effective local context (below) for local-pinned tiers

## Routing (first match wins)

For each `/v1/chat/completions` request:

1. `model` matches a tier id → that tier's policy. For `auto` tiers:
   `x-loxo-quality: best` header → cloud; estimated prompt tokens >
   the effective local context → cloud; else local.
2. `model` matches a `local_models` entry → local (explicit local intent)
3. `x-loxo-quality: best` → cloud
4. estimated prompt > the effective local context → cloud
5. `model` contains `/` → cloud (provider-prefixed ids are OpenRouter's
   convention)
6. default → local

## The context gate

Rules 1 and 4 above are one comparison — `estimate_prompt_tokens(body) >
effective_local_context()` — and both sides are derived rather than assumed.
The failure modes are asymmetric: underestimating the prompt or overstating
the limit sends an over-long request to a local model, which produces garbage
or crashes; erring the other way sends it to cloud, which costs money and
works. Both sides are therefore biased toward cloud.

**The estimate (left side).** `_count_prompt_chars` counts every request
field the chat template renders — not just `content`, but `tool_calls`,
`reasoning_content`, `tool_call_id`, `name`, and the `tools`/`functions`
schemas. Agentic clients send large tool definitions and tool-call histories;
on one replay case those fields were 87% of the real prompt and had been
counted as zero. The char total is divided by `ESTIMATE_CHARS_PER_TOKEN`, a
constant calibrated so the estimate never underestimates the reference
tokenizer on the replay corpus — see "Calibrating the divisor" below.

**The limit (right side).** `effective_local_context()` resolves in
precedence order, and `/health` reports which tier answered under
`local_context_source`:

| Source | Where it comes from |
|---|---|
| `config-explicit` | `LOCAL_CONTEXT_LIMIT` env or `local_context_limit` in TOML — an operator override, always wins |
| `probe` | The local backend's `/models`, read once at startup (`context_length` / vLLM's `max_model_len`); smallest wins |
| `hf-cache` | `max_position_embeddings` from the served model's `config.json` in the local HuggingFace cache |
| `legacy-default` | `60000` — the historical hardcoded value, now only a last resort |

The probe outranks the cache because it reflects the *serving* configuration
(vLLM started with a reduced `--max-model-len`), while the cache only knows
the model's native ceiling. Every derivation fails closed to the next tier:
an unreachable backend, an unmounted cache volume, or malformed JSON yields
`None`, never a wrong number. The cache read is bounded by
`HF_CACHE_READ_TIMEOUT` because some filesystem states block instead of
erroring — a macOS TCC consent prompt no launchd job can answer, an
unreachable network mount — and neither raises, so `try/except` never fires.
`llm-router-serve.sh` pins `HF_HOME` to the internal cache for the same
reason.

**Calibrating the divisor.** The corpus, the reference tokenizer, and the
calibration tooling live in the companion `llitmus-eval` repo, not here —
loxo's interpreter has neither `transformers` nor `huggingface_hub` and must
not gain them. `llitmus-eval/scripts/calibrate_router_divisor.py` prints a
recommended value (the corpus minimum ratio times a safety factor);
`llitmus-eval/tests/test_router_divisor_property.py` is the standing guard
that the pinned value never underestimates. `test_routing.py` pins the
constant itself, so changing it in loxo alone fails here and points at the
recalibration path. The calibration is only valid for tokenizers that segment
like the reference — cross-family variance (Qwen vs Llama vs Mistral) is far
larger than the corpus variance it was fitted to — so switching the local
model family means recalibrating.

**Family drift.** The two halves of the gate are automated to different
degrees: the limit follows the served model on its own, while the divisor is
pinned by hand. Point the router at a different model family and the right
side tracks reality while the left silently does not.
`divisor_family_match()` compares the served model's `model_type` — from the
same cached `config.json` the context tier reads, one bounded read, not two —
against `ESTIMATE_DIVISOR_REF_MODEL_TYPE`, logs one non-fatal line at startup
on a mismatch, and reports under `estimate_divisor` in `/health`:

```json
"estimate_divisor": {"chars_per_token": 3.5, "ref_model_type": "qwen3",
                     "served_model_type": "llama", "family_match": false}
```

`family_match` is tri-state: `null` means undetermined (no repo configured,
nothing in the cache, or a config without `model_type`) and is never reported
as agreement. It reports only — it never adjusts the divisor or forces cloud.
An estimate wrong by a family-sized factor is a recalibration job; silently
compensating at request time would hide the drift this check exists to
surface.

**Transport fallback (narrow by design).** If a local-routed request fails
at the transport level (connect refused/timeout, read error, or the local
server disconnecting before the first byte), it is retried against cloud
with the tier's `cloud_target` (or `cloud_default_model`). This exists only
for "local is physically down or wedged" — it is not a quality fallback.
Constraints:

- Streaming: fallback applies only before the first byte reaches the
  client; a partially-sent stream is never restarted.
- The pinned-`local` tier opts out entirely: it preflights reachability and
  prompt size and returns a descriptive 422 (`local_context_exceeded`,
  `local_unreachable`) instead of falling back — a hard-fail is preferred
  over silent cloud spend.

**Body adjustments in flight:** every streaming request — local or cloud —
gets `stream_options.include_usage` injected, so the final SSE chunk carries
token counts. Clients use them to display cost, and the adequacy ledger uses
them to record usage; without injection local streaming requests are logged
with null tokens.

## Vision policy

Applied when a request carries `image_url` content, per the tier's `vision`
setting:

- `native` — target model sees images; pass through
- `reject` — 422 with a message naming tiers that do have vision
- `shim` — transcribe each image to text via a local VLM
  (`VISION_SHIM_MODEL`) and substitute it, so text-only targets can "read"
  screenshots. Mode `auto` (default) escalates to a multimodal cloud model
  (`VISION_CLOUD_MODEL`) when the transcription is too thin to be useful
  (`VISION_OCR_MIN_CHARS`); `local`/`cloud` modes force one side.
  Per-request override: `x-loxo-vision` header.
- `local` — on-machine OCR only; refuses (422) rather than escalate,
  because escalating would break the tier's local pin

The shim degrades gracefully: a failed transcription leaves the original
image part untouched and never breaks the request.

## Spend tracking

- Every cloud-served response is scanned for OpenRouter's `usage.cost`:
  parsed from the JSON body (non-streaming) or teed out of the terminal SSE
  usage chunk (streaming) without delaying the client.
- Costs accumulate in memory (total, per provider, per model, plus per-model
  cached-token counts and estimated cache savings) and append to a JSONL
  ledger (`SPEND_LEDGER`, default `$LOXO_STATE_DIR/spend.jsonl`, where the
  state root resolves `LOXO_STATE_DIR` > `$XDG_STATE_HOME/loxo-llm-router`
  > `~/.local/state/loxo-llm-router`; a pre-existing legacy
  `~/.config/loxo-llm-router/spend.jsonl` keeps working, but only while it
  exists and the new path doesn't), which re-seeds the accumulator on
  startup. Ledger writes run in a worker thread off the event loop and are
  best-effort: a write failure never breaks a response (the cost is still
  counted in memory).
- A **rate card** (per-Mtok pricing, context length, input modalities for
  every distinct `cloud_target`) is fetched from OpenRouter on a TTL
  (`RATE_CARD_TTL`, default 24h). Fetches are non-blocking and best-effort;
  endpoints serve the cached snapshot and never await the network.

## Request classification and the adequacy ledger (v0.2)

Every `/v1/chat/completions` request is classified from observable shape
(`classify.py`: system-prompt fingerprints plus tool count; message count is
recorded as telemetry, not a decision input) into
`main | chore | compaction | unknown` — first match wins, `unknown` is the
default and deliberately visible; fingerprints are grounded in captured
harness bodies, never guessed (`CLASSIFIER_VERSION` bumps on any rule
change). One metadata-only JSONL entry per completed request lands in
`$LOXO_STATE_DIR/adequacy.jsonl` (`ledger.py`: `Observation`, `StreamScan`,
`AdequacyLedger`): class, route, outcome signals (finish reason, tool-call
JSON validity, token splits incl. cached/reasoning, latency, cost) — never
message content. One caveat on those token splits: `reasoning` is `null` on
local rows, because mlx omits `completion_tokens_details` entirely. Null means
*unmeasured*, not zero — local models do reason, sometimes heavily (a
Qwen3-14B title generation took 125s on 2026-07-27, nearly all of it
reasoning), the count simply is not reported. Cloud rows carry `reasoning: 0`
when the provider genuinely reports zero, so null and 0 must never be
normalized together: a `.tokens.reasoning // 0` downstream turns "we don't
know" into "there was none" and yields a confidently wrong answer.
Observe-only in v0.2: no routing decision reads it.
Exception paths (mid-stream disconnects, transport errors without fallback)
currently write no entry; an error-marker schema addition is planned before
the dial (v0.4) consumes this data.

## Cloud-side parity: cache, reasoning, metadata (v0.2)

Cloud-bound bodies get top-level `cache_control: {"type": "ephemeral"}`
injected (`cache.py`, mechanism decided by the 2026-07 spike — see
docs/spikes/2026-07-cache-affinity.md) when the live rate card says the
target supports cache reads; client-supplied `cache_control` is never
overridden. `/v1/spend` reports cached tokens and estimated savings per
model. Tiers may set `reasoning = "low|medium|high"`, mapped to OpenRouter's
`reasoning.effort`; a client-sent `reasoning` wins, and OpenAI-style
`reasoning_effort` is translated rather than dropped. `/v1/models` derives
tier `context_length` from the live rate card (explicit `advertised_context`
overrides; local-pinned tiers advertise `effective_local_context()`, so the
number clients see tracks the model actually being served) and advertises the
cloud target's per-token rates as a ceiling; local non-streaming responses
report `usage.cost: 0` (local streaming deliberately not rewritten).

## HTTP surface

| Endpoint | What it serves |
|---|---|
| `POST /v1/chat/completions` | The proxy path described above |
| `GET /v1/models` | Synthesized tier entries (with `context_length`) merged with both backends' live model lists |
| `GET /v1/spend` | Accumulated cloud spend: total, per provider, per model, plus the rate-card snapshot |
| `GET /health` | Config snapshot: backends, local models, `local_context_limit` + `local_context_source`, `estimate_divisor` (incl. `family_match`), vision policy, rate cards, spend summary |

## Auth

If `ROUTER_TOKEN` is set, every `/v1/*` endpoint requires
`Authorization: Bearer <token>` (constant-time compare) — it guards the
money-spending cloud path on a shared network. Client `Authorization`
headers are never forwarded upstream; the router substitutes its own
`OPENROUTER_API_KEY` on cloud requests.

## Deployment

Docker is the primary deployment (`Dockerfile` + `docker-compose.yml`;
env file carries secrets). `loxo-llm-router` is the console entry point;
systemd/launchd recipes live in `docs/deploy.md`.
