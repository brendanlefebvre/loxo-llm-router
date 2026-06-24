#!/bin/bash
set -eu
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin

PYTHON=/Users/brendanl/.venvs/mlx/bin/python3
ROUTER_DIR=/Users/brendanl/bin
PORT=9090
# uvicorn log config with UTC-timestamped formatters (lives in the repo).
LOGCONFIG=/Users/brendanl/src/llm-router/llm-router-logconfig.json

# Load credentials (OPENROUTER_API_KEY) from a 600-perm env file, not from the plist.
ENV_FILE=/Users/brendanl/.config/llm-router/env
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# Configuration
export TZ=UTC  # so uvicorn's %(asctime)s renders UTC, matching the router's own log timestamps
export LOCAL_BASE_URL="${LOCAL_BASE_URL:-http://localhost:7979/v1}"
export CLOUD_BASE_URL="${CLOUD_BASE_URL:-https://openrouter.ai/api/v1}"
export LOCAL_MODELS="${LOCAL_MODELS:-mlx-community/Qwen3.6-35B-A3B-4bit}"
export LOCAL_CONTEXT_LIMIT="${LOCAL_CONTEXT_LIMIT:-60000}"
export CLOUD_DEFAULT_MODEL="${CLOUD_DEFAULT_MODEL:-anthropic/claude-sonnet-4.6}"

exec "$PYTHON" -m uvicorn \
  --app-dir "$ROUTER_DIR" \
  --log-config "$LOGCONFIG" \
  --host 0.0.0.0 \
  --port "$PORT" \
  llm_router:app
