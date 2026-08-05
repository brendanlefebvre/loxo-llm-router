# AGENTS.md

FastAPI proxy (`loxo_llm_router/`) that routes OpenAI-compatible requests to a
local model server (:7979) or cloud (OpenRouter) based on intent heuristics.
Tests: `pytest test_routing.py test_config.py` (72); `pytest` for all 227.

## Running / deploying

- Run with any Python ≥3.11 that has the deps installed (`pip install .`).
- Deployment recipes (Docker / systemd / launchd) live in `docs/deploy.md`.
- Logs go to stdout/stderr; the process manager decides where they land. UTC
  formatting via `LOG_CONFIG=llm-router-logconfig.json`.

## Config / secrets

Config uses a three-layer precedence stack: **bundled defaults** < **`loxo.toml`**
< **environment variables**. Secrets (`OPENROUTER_API_KEY`, `ROUTER_TOKEN`) are
read only from the environment — never put them in `loxo.toml`. See
`loxo.toml.example` for the full schema and `.env.example` for environment options.

## Virtual models / Tiers

Tiers are defined in `[tiers.*]` in `loxo.toml`; each becomes the virtual id
`<namespace>/<key>` (default namespace: `loxo`). The virtual id is never forwarded
upstream. Clients send one virtual id; the router resolves it to the right backend
and model based on the tier's `routing` policy.

| Tier | routing | vision |
|---|---|---|
| `loxo/auto` | local-first, escalate on size/`x-loxo-quality: best` | shim |
| `loxo/fast` | pinned cloud | reject images (422) |
| `loxo/balanced` | pinned cloud | shim |
| `loxo/reason` | pinned cloud | native |
| `loxo/deep` | pinned cloud | native |
| `loxo/local` | pinned local, **hard-fail** (no cloud fallback) | local OCR only, else 422 |

Rebrand the whole namespace by setting `namespace` in `loxo.toml` (or `ROUTER_NS`).
Add/retarget tiers by editing the `[tiers.*]` table.

## Routing rules (first match wins)

0. `model` matches a tier id → resolve by its `routing` policy:
   `cloud` (pinned cloud target), `local` (pinned local, no cloud fallback), or
   `auto` (`x-loxo-quality: best` or oversized prompt → `cloud_target`, else local).
1. `model` matches a `local_models` entry → local
2. `x-loxo-quality: best` header → cloud
3. estimated prompt > the effective local context → cloud
4. `model` contains `/` → cloud
5. default → local

Both sides of rule 3 are derived, not assumed — see "The context gate" in
ARCHITECTURE.md. The estimate counts every field the chat template renders
(`tool_calls`, `reasoning_content`, tool schemas — agentic clients send a lot
of these) over a divisor calibrated in the companion `llitmus-eval` repo. The
limit resolves explicit config > startup `/models` probe > the served model's
HF-cache `config.json` > the legacy `60000`; `/health` reports which tier
answered under `local_context_source`. Note the probe never fires on MLX
(`mlx_lm.server`'s `/models` carries no context field), so `hf-cache` is the
effective tier there — and a constrained MLX serving window is invisible to
the chain and must be pinned as `local_context_limit` by hand. Changing
`ESTIMATE_CHARS_PER_TOKEN`
requires recalibrating — `test_routing.py` pins it and will fail if you don't.
`/health`'s `estimate_divisor.family_match` flags the case where the limit has
followed the served model to a new family but the divisor has not (`null` =
undetermined, never "fine"); it reports only and never changes routing.

Note: `x-loxo-quality: best` only affects the `auto` tier (rule 0 / rule 2). The
pinned tiers (`fast`/`deep`/`local`) return before the header is read, so the
header is a no-op on them — pick the tier directly (e.g. `loxo/deep`) instead.

If local is chosen but the connection fails/times out (`LOCAL_CONNECT_TIMEOUT`,
default 5s), the request is re-sent to cloud with `cloud_default_model`. This
is a transport fallback only, not a quality fallback. It applies on both the
streaming and non-streaming paths; on the streaming path it can only fire
before the first byte is sent (a partially-sent stream cannot be restarted).
A non-transport upstream error (e.g.
402/429/5xx) is never masked: streamed responses surface the real status instead
of a blank HTTP 200.

## Vision shim (opt-in)

Disabled unless `VISION_SHIM_MODEL` (local OCR) or `VISION_CLOUD_MODEL`
(multimodal cloud reroute) is set. When set, image content aimed at a
text-only target is handled per a 3-mode policy (`VISION_MODE`:
`auto`/`local`/`cloud`), per-request overridable via the `x-loxo-vision` header:

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
  `$LOXO_STATE_DIR/spend.jsonl`, where the state root resolves
  `LOXO_STATE_DIR` > `$XDG_STATE_HOME/loxo-llm-router` >
  `~/.local/state/loxo-llm-router` (legacy `~/.config/loxo-llm-router/spend.jsonl`
  still honored with a logged pointer, but only while it exists and the new
  path doesn't). Seeded into memory on startup so totals
  survive process restarts. Set to `""` to disable (in-memory only).

Note: OpenCode's own `$0.00` display is unchanged — it has no mechanism to read
cost from a custom provider. Use `curl localhost:9090/v1/spend` for the real figure.

The router also exposes a live **rate card** (price *before* spending) fetched
from OpenRouter pricing (`RATE_CARD_URL`, cached `RATE_CARD_TTL` seconds, lazy +
non-blocking). It appears under `rate_cards` in both `/health` and `/v1/spend`,
giving per-Mtok input/output/cache-read rates for each `cloud_target` alongside
the `usage.cost` actuals. On fetch, a text-only `cloud_target` whose virtual
model declares vision logs a one-line shim-required self-check.

## Verification

Unit tests: `pytest test_routing.py test_config.py` (72 tests); `pytest` runs
the full suite (227). To verify manually:
- `curl http://localhost:9090/v1/models` (or a chat completions POST)
- `curl http://localhost:9090/health | jq '{local_context_limit, local_context_source, estimate_divisor}'`
  — which tier the context gate resolved from, and whether the token estimate
  is still calibrated for the model being served
- `curl http://localhost:9090/v1/spend` — check cloud spend totals
- Check process manager logs (stdout/stderr) for tracebacks.
- `ROUTER_QUIET` defaults to off — per-request routing decisions log to stderr.
