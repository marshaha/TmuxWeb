#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export TMUX_WEB_HOST="${TMUX_WEB_HOST:-127.0.0.1}"
export TMUX_WEB_PORT="${TMUX_WEB_PORT:-8787}"
: "${TMUX_WEB_PASSWORD:?Set TMUX_WEB_PASSWORD in .env or the environment}"
: "${TMUX_WEB_SESSION_SECRET:?Set TMUX_WEB_SESSION_SECRET in .env or the environment}"

exec python3 -m uvicorn tmux_web:create_app --factory --host "$TMUX_WEB_HOST" --port "$TMUX_WEB_PORT"
