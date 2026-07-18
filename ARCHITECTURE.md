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
  defaults to the local context limit for local-pinned tiers

## Routing (first match wins)

For each `/v1/chat/completions` request:

1. `model` matches a tier id → that tier's policy. For `auto` tiers:
   `x-quality: best` header → cloud; estimated prompt tokens >
   `local_context_limit` → cloud; else local.
2. `model` matches a `local_models` entry → local (explicit local intent)
3. `x-quality: best` → cloud
4. estimated prompt > `local_context_limit` → cloud (the estimate counts
   tool/function schemas too — agentic clients send large tool definitions)
5. `model` contains `/` → cloud (provider-prefixed ids are OpenRouter's
   convention)
6. default → local

Token estimation is ~4 chars/token — deliberately cheap, used only for
threshold gating.

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

**Body adjustments in flight:** cloud-bound streaming requests get
`stream_options.include_usage` injected (so clients receive token counts in
the final SSE chunk); local-bound requests get it stripped (local servers
may not accept it).

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
  Per-request override: `x-vision` header.
- `local` — on-machine OCR only; refuses (422) rather than escalate,
  because escalating would break the tier's local pin

The shim degrades gracefully: a failed transcription leaves the original
image part untouched and never breaks the request.

## Spend tracking

- Every cloud-served response is scanned for OpenRouter's `usage.cost`:
  parsed from the JSON body (non-streaming) or teed out of the terminal SSE
  usage chunk (streaming) without delaying the client.
- Costs accumulate in memory (total, per provider, per model) and append to
  a JSONL ledger (`SPEND_LEDGER`, default `$LOXO_STATE_DIR/spend.jsonl`, where
  the state root resolves `LOXO_STATE_DIR` > `$XDG_STATE_HOME/loxo-llm-router`
  > `~/.local/state/loxo-llm-router`; a pre-existing legacy
  `~/.config/loxo-llm-router/spend.jsonl` keeps working, but only while it
  exists and the new path doesn't), which re-seeds the accumulator on
  startup. Ledger writes are best-effort: a write failure never breaks a
  response (the cost is still counted in memory).
- A **rate card** (per-Mtok pricing, context length, input modalities for
  every distinct `cloud_target`) is fetched from OpenRouter on a TTL
  (`RATE_CARD_TTL`, default 24h). Fetches are non-blocking and best-effort;
  endpoints serve the cached snapshot and never await the network.

## HTTP surface

| Endpoint | What it serves |
|---|---|
| `POST /v1/chat/completions` | The proxy path described above |
| `GET /v1/models` | Synthesized tier entries (with `context_length`) merged with both backends' live model lists |
| `GET /v1/spend` | Accumulated cloud spend: total, per provider, per model, plus the rate-card snapshot |
| `GET /health` | Config snapshot: backends, local models, vision policy, rate cards, spend summary |

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
