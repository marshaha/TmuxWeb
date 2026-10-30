#!/usr/bin/env bash
# Supervisor for the console: restarts the service when it exits and records *why* it exited.
# tmux closes a session as soon as its pane process ends, so without this every unexpected exit
# looks like "the session vanished" with no evidence left behind.
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
PORT_FILE="$LOGDIR/service-exit.log"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

# Record every session closure: "#{session_name}" resolves to the wrong session inside a hook,
# "#{hook_session_name}" is the one that just went away.
tmux set-hook -g session-closed \
  'run-shell "echo \"$(date -Is) closed session=#{hook_session_name}\" >> /tmp/tmux-session-closed.log"' \
  2>/dev/null || true

while true; do
  started=$(date -Is)
  pid=$$
  echo "[$started] starting uvicorn (supervisor $pid) on port ${TMUX_WEB_PORT:-8787}" >>"$LOGDIR/service.log"
  ./.venv/bin/python -m uvicorn tmux_web:create_app --factory \
    --host 0.0.0.0 --port "${TMUX_WEB_PORT:-8787}" >>"$LOGDIR/service.log" 2>&1 &
  child=$!
  # The password (and the other settings) are read from .env only at startup, so editing .env while the
  # service runs used to leave the old value in memory — the new password simply would not log in.
  # Watch the file and recycle the child; the loop restarts it immediately.
  (
    stamp=$(stat -c %Y ./.env 2>/dev/null || echo 0)
    while kill -0 "$child" 2>/dev/null; do
      sleep 2
      now=$(stat -c %Y ./.env 2>/dev/null || echo 0)
      if [ "$now" != "$stamp" ]; then
        echo "[$(date -Is)] .env changed, restarting uvicorn pid=$child" >>"$LOGDIR/service.log"
        kill -TERM "$child" 2>/dev/null
        break
      fi
    done
  ) &
  watcher=$!
  wait "$child"
  code=$?
  kill "$watcher" 2>/dev/null || true
  # 137 = SIGKILL (someone/kernel), 143 = SIGTERM, 130 = SIGINT, 129 = SIGHUP, 139 = segfault
  echo "[$(date -Is)] uvicorn pid=$child exited code=$code (started $started)" >>"$PORT_FILE"
  echo "[$(date -Is)] uvicorn pid=$child exited code=$code (started $started)" >>"$LOGDIR/service.log"
  sleep 2
done
