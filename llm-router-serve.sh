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

# Bare `python3` resolves against PATH, and a service manager's PATH is not your
# shell's. Under launchd on macOS, /usr/bin/python3 is a stub that dispatches to
# the active Xcode toolchain, which has no site-packages -- so this dies with a
# bare "No module named uvicorn" and gets respawned on a KeepAlive loop forever.
# Check up front and say what to do about it. Set PYTHON in the env file to pin
# a specific interpreter.
if ! "$PYTHON" -c 'import uvicorn' >/dev/null 2>&1; then
  echo "FATAL: interpreter '$PYTHON' cannot import uvicorn." >&2
  echo "       resolved to: $(command -v "$PYTHON" 2>/dev/null || echo "$PYTHON")" >&2
  echo "       Set PYTHON=/abs/path/to/python3 in $ENV_FILE (or the environment)" >&2
  echo "       to an interpreter that has this project's dependencies installed." >&2
  exit 78  # EX_CONFIG
fi

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
