#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [PUBLIC_BASE_URL]

Fast-forward origin/main, restart tmux-web-console, and verify /login.
PUBLIC_BASE_URL is optional; for example: https://example.com

Environment overrides:
  TMUX_WEB_DEPLOY_REMOTE  Git remote (default: origin)
  TMUX_WEB_DEPLOY_BRANCH  Git branch (default: main)
  TMUX_WEB_TMUX_SESSION   Service tmux session (default: tmux-web-console)
  TMUX_WEB_PUBLIC_URL     Public base URL when no positional URL is supplied
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if (( $# > 1 )); then
  usage >&2
  exit 2
fi

REMOTE="${TMUX_WEB_DEPLOY_REMOTE:-origin}"
BRANCH="${TMUX_WEB_DEPLOY_BRANCH:-main}"
SESSION="${TMUX_WEB_TMUX_SESSION:-tmux-web-console}"
PUBLIC_URL="${1:-${TMUX_WEB_PUBLIC_URL:-}}"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "Refusing to deploy: the worktree has uncommitted changes." >&2
  git status --short >&2
  exit 2
fi

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  echo "Refusing to deploy branch '$current_branch'; expected '$BRANCH'." >&2
  exit 2
fi

old_commit="$(git rev-parse HEAD)"
git fetch "$REMOTE" "$BRANCH"
read -r ahead behind < <(git rev-list --left-right --count "HEAD...$REMOTE/$BRANCH")
if (( ahead > 0 )); then
  echo "Refusing to deploy: local branch is $ahead commit(s) ahead of $REMOTE/$BRANCH." >&2
  exit 2
fi
if (( behind > 0 )); then
  git merge --ff-only "$REMOTE/$BRANCH"
fi
new_commit="$(git rev-parse HEAD)"

if [[ ! -f .env ]]; then
  echo "Missing $ROOT/.env" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
port="${TMUX_WEB_PORT:-8787}"

# Restart only the Web service; existing managed tmux sessions are untouched.
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" "$ROOT/start_tmux_web.sh"

local_url="http://127.0.0.1:$port/login"
ready=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 -o /dev/null "$local_url"; then
    ready=1
    break
  fi
  sleep 1
done
if (( ready == 0 )); then
  echo "Deployment failed: $local_url did not become ready." >&2
  tmux capture-pane -p -t "$SESSION" -S -120 >&2 || true
  exit 1
fi

if [[ -n "$PUBLIC_URL" ]]; then
  public_login="${PUBLIC_URL%/}/login"
  if ! curl -fsS --max-time 15 -o /dev/null "$public_login"; then
    echo "Deployment started locally, but public verification failed: $public_login" >&2
    exit 1
  fi
fi

printf 'Deployed %s\n' "$new_commit"
if [[ "$old_commit" == "$new_commit" ]]; then
  echo "Source was already current; the Web service was restarted."
else
  printf 'Updated %s -> %s\n' "$old_commit" "$new_commit"
fi
printf 'Local:  %s\n' "$local_url"
if [[ -n "$PUBLIC_URL" ]]; then
  printf 'Public: %s\n' "${PUBLIC_URL%/}/login"
fi
