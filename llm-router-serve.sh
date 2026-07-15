#!/usr/bin/env bash
set -euo pipefail

# Repo dir derived from this script's own location — no hardcoded paths.
# readlink -f is required: BASH_SOURCE holds the path as invoked, so when this is
# run through a symlink (e.g. ~/bin/llm-router-serve.sh) an unresolved dirname
# yields the symlink's directory, not the repo, and --app-dir below then points
# somewhere with no loxo_llm_router package.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# Optional env file (secrets/overrides; chmod 600). Override path with LOXO_ENV_FILE.
ENV_FILE="${LOXO_ENV_FILE:-$HOME/.config/loxo-llm-router/env}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

export TZ="${TZ:-UTC}"
PYTHON="${PYTHON:-python3}"

# UTC-timestamped uvicorn formatters ship in the repo; use if present.
LOG_CONFIG="${LOG_CONFIG:-$SCRIPT_DIR/llm-router-logconfig.json}"
LOG_ARGS=()
[ -f "$LOG_CONFIG" ] && LOG_ARGS=(--log-config "$LOG_CONFIG")

# Host/port resolve from config; allow shell overrides too.
exec "$PYTHON" -m uvicorn \
  --app-dir "$SCRIPT_DIR" \
  "${LOG_ARGS[@]}" \
  --host "${LOXO_HOST:-${HOST:-0.0.0.0}}" \
  --port "${LOXO_PORT:-${PORT:-9090}}" \
  loxo_llm_router:app
