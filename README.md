# llm-router

OpenAI-compatible proxy that dispatches each request to a **local** (MLX /
`mlx_vlm`) endpoint or a **cloud** (OpenRouter) endpoint based on
intent-expressing heuristics — not retry-on-failure.

Part of the local AI inference stack on the Mac Mini:
`mlx_vlm (:7979)` → `llm_router (:9090)` → OpenCode / any OpenAI-compatible client.

## Files
- `llm_router.py` — FastAPI proxy, run via uvicorn on `0.0.0.0:9090`
- `llm-router-serve.sh` — launch wrapper (invoked by the `com.local.llm-router` LaunchAgent)

## Routing rules (first match wins)
1. `model` matches a `LOCAL_MODELS` entry → LOCAL
2. `x-quality: best` header → CLOUD
3. estimated prompt > `LOCAL_CONTEXT_LIMIT` tokens → CLOUD
4. `model` contains `/` (provider-prefixed) → CLOUD
5. default → LOCAL

Plus a hard-error fallback: if LOCAL is chosen but unreachable, forward to
CLOUD with `CLOUD_DEFAULT_MODEL`.

## Config (env, set in the serve script or `~/.config/llm-router/env`)
`LOCAL_BASE_URL` · `CLOUD_BASE_URL` · `OPENROUTER_API_KEY` · `LOCAL_MODELS` ·
`LOCAL_CONTEXT_LIMIT` · `CLOUD_DEFAULT_MODEL`

## Secrets
`OPENROUTER_API_KEY` lives **outside** this repo at `~/.config/llm-router/env`
(chmod 600), loaded by the serve script. Never commit it — `.gitignore` guards against it.
