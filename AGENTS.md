# AGENTS.md

Single-file FastAPI proxy (`llm_router.py`) that routes OpenAI-compatible
requests to a local MLX endpoint (:7979) or cloud (OpenRouter) based on
intent heuristics. No build, no tests, no requirements file.

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

## Routing rules (first match wins)

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

Disabled unless `VISION_SHIM_MODEL` is set. When set, image content aimed at a
text-only target is transcribed via a local VLM (`VISION_SHIM_URL`), with a
3-mode policy (`VISION_MODE`: `auto`/`local`/`cloud`) per-request overridable
via the `x-vision` header. `VISION_CAPABLE_MODELS` skips the shim for models
that already handle images.

## Verification

No automated tests. To verify changes manually:
- `curl http://localhost:9090/v1/models` (or a chat completions POST)
- Tail `~/Library/Logs/llm-router/err.log` for tracebacks.
- `ROUTER_QUIET` defaults to off — per-request routing decisions log to `out.log`.
