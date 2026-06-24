# AGENTS.md

Single-file FastAPI proxy (`llm_router.py`) that routes OpenAI-compatible
requests to a local MLX endpoint (:7979) or cloud (OpenRouter) based on
intent heuristics. No build step; a minimal `pytest` covers routing
(`test_routing.py`); no requirements file.

## Running / deploying

- Both deployed files are symlinked to this repo:
  `~/bin/llm_router.py` and `~/bin/llm-router-serve.sh` → repo copies.
  Edits take effect on process restart.
- Managed by LaunchAgent `com.local.llm-router`. Restart after edits:
  `launchctl kickstart -k gui/$(id -u)/com.local.llm-router`
- Logs: `~/Library/Logs/llm-router/out.log` and `err.log`.
- Interpreter is hardcoded in the serve script:
  `/Users/brendanl/.venvs/mlx/bin/python3`. Deps (install manually):
  `pip install fastapi uvicorn httpx`.

## Config / secrets

- All config via env vars; defaults live in the `llm_router.py` docstring.
  The serve script sets a few (`LOCAL_BASE_URL`, `CLOUD_BASE_URL`,
  `LOCAL_MODELS`, `LOCAL_CONTEXT_LIMIT`, `CLOUD_DEFAULT_MODEL`) and leaves
  the rest to code defaults.
- `OPENROUTER_API_KEY` (and optional `ROUTER_TOKEN`) live in
  `~/.config/llm-router/env` (chmod 600, gitignored). Never commit.

## Virtual models

The router owns a `VirtualModel` registry (`VIRTUAL_MODELS` in `llm_router.py`).
Clients send a single virtual id (default `airwolf/auto`); the router resolves it
to real upstream models via the routing rules below — `cloud_target`
(`z-ai/glm-5.2`) when routed to cloud, the first `LOCAL_MODELS` entry when local.
The virtual id is never forwarded upstream. This is why the OpenCode config
declares one honest `airwolf/auto` entry with `attachment: true` (true of the
pipeline — the router shims/reroutes images) instead of lying about a specific
model. Override ids via `AUTO_MODEL_ID` / `AUTO_CLOUD_MODEL` / `AUTO_LOCAL_MODEL`.

### Tiers (archetypes)

Beyond the default `airwolf/auto`, the registry advertises three pinned tiers so
client agents can pick a cost/quality lane per task:

| Tier | routing | target | vision |
|---|---|---|---|
| `airwolf/auto` | local-first, escalate on size/`best` | local → `z-ai/glm-5.2` | shim |
| `airwolf/fast` | pinned cloud | `z-ai/glm-4.7-flash` | reject images (422) |
| `airwolf/deep` | pinned cloud | `google/gemini-2.5-pro` | native |
| `airwolf/local` | pinned local, **hard-fail** (no cloud fallback) | first `LOCAL_MODELS` | local OCR only, else 422 |

Pinned-local hard-fails (oversized prompt, local down, or unreadable image)
return HTTP 422 with a message naming the remedy — switch to `airwolf/auto` or
`airwolf/deep`. Override ids/targets via `FAST_*`, `DEEP_*`, `LOCAL_TIER_*` env
vars (see the `llm_router.py` docstring).

## Routing rules (first match wins)

0. `model` matches a `VIRTUAL_MODELS` id → resolve by its `routing` policy:
   `cloud` (pinned cloud target), `local` (pinned local, no cloud fallback), or
   `auto` (`x-quality: best` or oversized prompt → `cloud_target`, else local).
1. `model` matches a `LOCAL_MODELS` tag → LOCAL
2. `x-quality: best` header → CLOUD
3. estimated prompt > `LOCAL_CONTEXT_LIMIT` tokens → CLOUD
4. `model` contains `/` → CLOUD
5. default → LOCAL

If LOCAL is chosen but the connection fails/times out (`LOCAL_CONNECT_TIMEOUT`,
default 5s), the request is re-sent to CLOUD with `CLOUD_DEFAULT_MODEL`. This
is a transport fallback only, not a quality fallback, and only works for
streaming before the first byte is sent.

## Vision shim (opt-in)

Disabled unless `VISION_SHIM_MODEL` (local OCR) or `VISION_CLOUD_MODEL`
(multimodal cloud reroute) is set. When set, image content aimed at a
text-only target is handled per a 3-mode policy (`VISION_MODE`:
`auto`/`local`/`cloud`), per-request overridable via the `x-vision` header:

- `local` — OCR each image via a local VLM (`VISION_SHIM_URL`), feed the
  resulting text to the text-only model.
- `cloud` — reroute the whole request (images intact) to `VISION_CLOUD_MODEL`,
  a multimodal cloud model.
- `auto` — OCR locally; if the transcription is thin
  (`< VISION_OCR_MIN_CHARS` chars, likely a non-text image), escalate to
  cloud vision via `VISION_CLOUD_MODEL`.

`VISION_CAPABLE_MODELS` skips the shim for models that already handle images.

## Cloud spend tracking

The router tracks the actual USD cost of every cloud request by reading
`usage.cost` from OpenRouter responses (always present; no request flags needed).

- `GET /v1/spend` — returns `total_usd`, `requests`, `since`, `ledger`
  (path string, or null if disabled), and a `by_provider` → `by_model`
  breakdown (provider = API hostname, e.g. `openrouter.ai`). Auth-gated
  like other endpoints.
- `GET /health` — includes a compact `spend` summary.
- Per-request log line: `[router] cloud cost=$X.XXXXXX provider=... model=... total=$Y.YYYYYY`
- `SPEND_LEDGER` (env var) — path to append-only JSONL ledger; default
  `~/.config/llm-router/spend.jsonl`. Seeded into memory on startup so totals
  survive LaunchAgent restarts. Set to `""` to disable (in-memory only).

Note: OpenCode's own `$0.00` display is unchanged — it has no mechanism to read
cost from a custom provider. Use `curl localhost:9090/v1/spend` for the real figure.

The router also exposes a live **rate card** (price *before* spending) fetched
from OpenRouter pricing (`RATE_CARD_URL`, cached `RATE_CARD_TTL` seconds, lazy +
non-blocking). It appears under `rate_cards` in both `/health` and `/v1/spend`,
giving per-Mtok input/output/cache-read rates for each `cloud_target` alongside
the `usage.cost` actuals. On fetch, a text-only `cloud_target` whose virtual
model declares vision logs a one-line shim-required self-check.

## Verification

Unit tests in `test_routing.py` cover routing logic. To verify manually:
- `curl http://localhost:9090/v1/models` (or a chat completions POST)
- `curl http://localhost:9090/v1/spend` — check cloud spend totals
- Tail `~/Library/Logs/llm-router/err.log` for tracebacks.
- `ROUTER_QUIET` defaults to off — per-request routing decisions log to `out.log`.
