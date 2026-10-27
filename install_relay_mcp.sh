#!/usr/bin/env bash
# Install the TmuxWeb agent-relay MCP into every agent CLI found on this machine.
#
#   ./install_relay_mcp.sh              # HTTP mode: clients point at /mcp, no python3 on their side
#   ./install_relay_mcp.sh --stdio      # legacy: every client spawns `python3 mcp_relay.py`
#   ./install_relay_mcp.sh --dry-run    # print the commands, token masked
#   ./install_relay_mcp.sh --uninstall  # remove it again
#
# Needs TMUXWEB_URL (default http://127.0.0.1:8787) and TMUXWEB_TOKEN; when the
# token is missing the script reads TMUX_WEB_API_TOKEN from ./.env.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="tmuxweb"
PYTHON="${TMUXWEB_PYTHON:-$(command -v python3)}"
SERVER="$DIR/mcp_relay.py"
MODE="http"
case "${1:-}" in
  ""|--http) MODE="http" ;;
  --stdio) MODE="stdio" ;;
  --dry-run) MODE="dry-run" ;;
  --uninstall) MODE="uninstall" ;;
  *) echo "usage: $0 [--http|--stdio|--dry-run|--uninstall]" >&2; exit 2 ;;
esac

if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi
URL="${TMUXWEB_URL:-http://127.0.0.1:8787}"
MCP_URL="${TMUXWEB_MCP_URL:-${URL%/}/mcp}"
TOKEN="${TMUXWEB_TOKEN:-${TMUX_WEB_API_TOKEN:-}}"
case "$(printf '%s' "${TMUX_WEB_MCP_AUTH:-on}" | tr '[:upper:]' '[:lower:]')" in
  0|off|no|false|none) MCP_AUTH="off" ;;
  *) MCP_AUTH="on" ;;
esac

if [ "$MCP_AUTH" = "on" ] && [ -z "$TOKEN" ]; then
  echo "error: set TMUXWEB_TOKEN (or TMUX_WEB_API_TOKEN in $DIR/.env)" >&2
  exit 1
fi

# keep the token out of terminal scrollback and CI logs
redact() { printf '%s' "$*" | sed -e "s/$TOKEN/***REDACTED***/g"; }
run() {
  if [ "$MODE" = "dry-run" ]; then printf '+ %s\n' "$(redact "$*")"; else "$@"; fi
}
have() { command -v "$1" >/dev/null 2>&1; }
kimi_config="${KIMI_CODE_HOME:-$HOME/.kimi-code}/mcp.json"

case "$MODE" in
  http|stdio|dry-run)
    if [ "$MODE" = "stdio" ]; then echo "mode: stdio ($PYTHON $SERVER)"; else echo "mode: http ($MCP_URL)"; fi
    if [ "$MODE" != "dry-run" ]; then
      echo "checking $URL ..."
      TMUXWEB_URL="$URL" TMUXWEB_TOKEN="$TOKEN" "$PYTHON" "$SERVER" --selftest | tail -2
    fi

    # ---- Claude Code: HTTP transport with an Authorization header
    if have claude; then
      if claude mcp get "$NAME" >/dev/null 2>&1; then
        echo "claude: $NAME already registered"
      elif [ "$MODE" = "stdio" ]; then
        run claude mcp add -s user "$NAME" --env "TMUXWEB_URL=$URL" --env "TMUXWEB_TOKEN=$TOKEN" \
          -- "$PYTHON" "$SERVER"
      else
        if [ "$MCP_AUTH" = "on" ]; then
          run claude mcp add -s user "$NAME" --transport http "$MCP_URL" --header "Authorization: Bearer $TOKEN"
        else
          run claude mcp add -s user "$NAME" --transport http "$MCP_URL"
        fi
      fi
    else
      echo "claude: not installed, skipping"
    fi

    # ---- Codex: streamable HTTP; the header has no CLI flag, so write it into config.toml
    if have codex; then
      if codex mcp get "$NAME" >/dev/null 2>&1; then
        echo "codex: $NAME already registered"
      elif [ "$MODE" = "stdio" ]; then
        run codex mcp add "$NAME" --env "TMUXWEB_URL=$URL" --env "TMUXWEB_TOKEN=$TOKEN" -- "$PYTHON" "$SERVER"
      else
        run codex mcp add "$NAME" --url "$MCP_URL"
        if [ "$MODE" != "dry-run" ] && [ "$MCP_AUTH" = "on" ]; then
          "$PYTHON" - "$HOME/.codex/config.toml" "$NAME" "$TOKEN" <<'PY'
import re, sys
from pathlib import Path

path, name, token = sys.argv[1:4]
file = Path(path)
text = file.read_text()
header = 'http_headers = { Authorization = "Bearer %s" }' % token
block = re.search(r"\[mcp_servers\.%s\]\n(?:.*\n)*?(?=\[|\Z)" % re.escape(name), text)
if not block:
    sys.exit("error: [mcp_servers.%s] not found in %s" % (name, path))
segment = block.group(0)
if re.search(r"^http_headers = .*$", segment, re.M):
    segment_new = re.sub(r"^http_headers = .*$", header, segment, flags=re.M)
else:
    segment_new = re.sub(r"(\[mcp_servers\.%s\]\n)" % re.escape(name), r"\1" + header + "\n", segment, count=1)
file.write_text(text.replace(segment, segment_new))
file.chmod(0o600)
print("codex: Authorization header written to %s" % path)
PY
        fi
      fi
    else
      echo "codex: not installed, skipping"
    fi

    # ---- Hermes: its HTTP mode asks for the token interactively (no --yes), so keep stdio
    if have hermes; then
      if hermes mcp list 2>/dev/null | grep -q "^$NAME\b"; then
        echo "hermes: $NAME already registered"
      elif [ "$MODE" = "dry-run" ]; then
        printf '+ hermes mcp add %s --url %s  (answers "no auth", then "y")\n' "$NAME" "$MCP_URL"
      elif [ "$MCP_AUTH" = "off" ]; then
        # no token to type in: answer "no authentication" to the prompt, then enable the tools
        printf 'n\ny\n' | hermes mcp add "$NAME" --url "$MCP_URL" \
          || echo "hermes: registration failed, see the message above"
      else
        printf 'y\n' | hermes mcp add "$NAME" --command "$PYTHON" --env "TMUXWEB_URL=$URL" \
          "TMUXWEB_TOKEN=$TOKEN" --args "$SERVER" \
          || echo "hermes: registration failed, see the message above"
      fi
    else
      echo "hermes: not installed, skipping"
    fi

    # ---- Kimi Code: ~/.kimi-code/mcp.json, Claude-shaped (url + headers, or command + env)
    if [ -d "${KIMI_CODE_HOME:-$HOME/.kimi-code}" ]; then
      if [ "$MODE" = "dry-run" ]; then
        echo "+ merge $NAME into $kimi_config"
      else
        [ "$MCP_AUTH" = "off" ] && TOKEN=""
        "$PYTHON" - "$kimi_config" "$NAME" "$PYTHON" "$SERVER" "$URL" "$MCP_URL" "$TOKEN" "$MODE" <<'PY'
import json, sys
from pathlib import Path

path, name, python, server, url, mcp_url, token, mode = sys.argv[1:9]
file = Path(path)
data = {}
if file.exists():
    try:
        data = json.loads(file.read_text() or "{}")
    except json.JSONDecodeError:
        sys.exit("error: %s is not valid JSON, not touching it" % file)
servers = data.setdefault("mcpServers", {})
if mode == "stdio":
    servers[name] = {"command": python, "args": [server],
                     "env": {"TMUXWEB_URL": url, "TMUXWEB_TOKEN": token}}
elif token:
    servers[name] = {"url": mcp_url, "headers": {"Authorization": "Bearer " + token}}
else:
    servers[name] = {"url": mcp_url}
file.parent.mkdir(parents=True, exist_ok=True)
file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
file.chmod(0o600)
print("kimi: %s written (%s, %s)" % (name, file, mode))
PY
      fi
    else
      echo "kimi: ${KIMI_CODE_HOME:-$HOME/.kimi-code} not found, skipping"
    fi

    if [ "$MODE" != "dry-run" ]; then
      for file in "$HOME/.codex/config.toml" "$HOME/.claude.json" "$kimi_config"; do
        [ -f "$file" ] && chmod 600 "$file" 2>/dev/null || true
      done
    fi

    echo
    echo "Other machines need neither this repo nor python3: point their MCP client at"
    if [ "$MCP_AUTH" = "on" ]; then
      echo "  $MCP_URL   with header  Authorization: Bearer <token>"
    else
      echo "  $MCP_URL   (no header needed: TMUX_WEB_MCP_AUTH=off, keep it on a trusted network)"
    fi
    ;;

  uninstall)
    have claude && claude mcp remove -s user "$NAME" >/dev/null 2>&1 || true
    have codex && codex mcp remove "$NAME" >/dev/null 2>&1 || true
    have hermes && hermes mcp remove "$NAME" >/dev/null 2>&1 || true
    if [ -f "$kimi_config" ]; then
      "$PYTHON" - "$kimi_config" "$NAME" <<'PY'
import json, sys
from pathlib import Path
path, name = sys.argv[1:3]
file = Path(path)
data = json.loads(file.read_text() or "{}")
data.get("mcpServers", {}).pop(name, None)
file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print("kimi: %s removed" % name)
PY
    fi
    echo "uninstalled $NAME"
    ;;

  *)
    echo "usage: $0 [--http|--stdio|--dry-run|--uninstall]" >&2
    exit 2
    ;;
esac
