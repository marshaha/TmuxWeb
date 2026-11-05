#!/usr/bin/env bash
# Install the TmuxWeb agent-relay MCP into every agent CLI found on this machine.
#
#   ./install_relay_mcp.sh              # auto: tailnet address if reachable, else stdio over ssh
#   ./install_relay_mcp.sh --http       # force HTTP (/mcp), no python3 on the client side
#   ./install_relay_mcp.sh --stdio      # legacy local stdio: clients spawn `python3 mcp_relay.py`
#   ./install_relay_mcp.sh --hub user@robo   # the machine that cannot reach 8787: register
#                                            # stdio over ssh against this hub instead
#   ./install_relay_mcp.sh --dry-run    # print the commands, token masked
#   ./install_relay_mcp.sh --uninstall  # remove it again
#
# Needs TMUXWEB_URL (default: this machine's tailnet address, e.g. http://100.x.y.z:8787,
# falling back to http://127.0.0.1:8787) and TMUXWEB_TOKEN; when the
# token is missing the script reads TMUX_WEB_API_TOKEN from ./.env.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="tmuxweb"
PYTHON="${TMUXWEB_PYTHON:-$(command -v python3)}"
SERVER="$DIR/mcp_relay.py"
MODE="auto"
HUB="${TMUXWEB_HUB:-}"
HUB_REPO="${TMUXWEB_HUB_REPO:-~/workspace/TmuxWeb}"
HUB_URL="${TMUXWEB_HUB_URL:-http://127.0.0.1:8787}"
ASSUME_UNREACHABLE="${TMUXWEB_ASSUME_UNREACHABLE:-0}"
while [ $# -gt 0 ]; do
  case "$1" in
    --http) MODE="http" ; shift ;;
    --stdio) MODE="stdio" ; shift ;;
    --auto) MODE="auto" ; shift ;;
    --dry-run) MODE="dry-run" ; shift ;;
    --uninstall) MODE="uninstall" ; shift ;;
    --hub) HUB="$2" ; shift 2 ;;
    --hub-repo) HUB_REPO="$2" ; shift 2 ;;
    --hub-url) HUB_URL="$2" ; shift 2 ;;
    --assume-unreachable) ASSUME_UNREACHABLE=1 ; shift ;;
    *) echo "usage: $0 [--http|--stdio|--auto|--dry-run|--uninstall] [--hub user@host] [--hub-repo PATH] [--hub-url URL]" >&2; exit 2 ;;
  esac
done

if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi
# 地址不限定 Tailscale：EasyTier / ZeroTier / WireGuard / 子网代理 / 物理局域网都算，
# 判据只有一条 —— 从这台机器能双向访问到 8787。scripts/relay-endpoint.sh 按
# 「显式 URL → overlay 网卡 → 物理局域网 → 127.0.0.1」的顺序探测，取第一个真能通的。
ENDPOINT_HELPER="$DIR/scripts/relay-endpoint.sh"
DISCOVERED=""
if [ -z "${TMUXWEB_URL:-}" ] && [ -f "$ENDPOINT_HELPER" ]; then
  DISCOVERED=$(TMUX_WEB_PORT="${TMUX_WEB_PORT:-8787}" bash "$ENDPOINT_HELPER" --probe 2>/dev/null || true)
fi
URL="${TMUXWEB_URL:-${DISCOVERED:-http://127.0.0.1:8787}}"
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

# 能不能就从这里访问到 /mcp？探测失败但没给 --hub 时保持 HTTP（照旧，只是提示一句）
reachable() {
  command -v curl >/dev/null 2>&1 || return 0
  [ "$ASSUME_UNREACHABLE" = "1" ] && return 1
  # --noproxy：探测必须测直连，代理会把别的网段答成 200/502
  code=$(curl -s --noproxy '*' -m 4 -o /dev/null -w '%{http_code}' "${1%/}/" 2>/dev/null || true)
  case "$code" in
    200|204|301|302|303|307|308|401|403) return 0 ;;
    *) return 1 ;;
  esac
}
TRANSPORT="http"
if [ "$MODE" = "stdio" ]; then TRANSPORT="stdio"; fi
if [ "$MODE" = "http" ] || [ "$MODE" = "auto" ] || [ "$MODE" = "dry-run" ]; then
  if ! reachable "$URL"; then
    if [ -n "$HUB" ]; then
      TRANSPORT="stdio-ssh"
    else
      echo "warning: $URL is not reachable from here; pass --hub user@host to register stdio over ssh" >&2
    fi
  fi
fi

# keep the token out of terminal scrollback and CI logs
redact() { printf '%s' "$*" | sed -e "s/$TOKEN/***REDACTED***/g"; }
run() {
  if [ "$MODE" = "dry-run" ]; then printf '+ %s\n' "$(redact "$*")"; else "$@"; fi
}
have() { command -v "$1" >/dev/null 2>&1; }
kimi_config="${KIMI_CODE_HOME:-$HOME/.kimi-code}/mcp.json"

SSH_BIN="${TMUXWEB_SSH_BIN:-ssh}"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
[ -n "${TMUXWEB_SSH_IDENTITY:-}" ] && SSH_OPTS+=(-i "$TMUXWEB_SSH_IDENTITY")
hub() { "$SSH_BIN" "${SSH_OPTS[@]}" "$HUB" "$@"; }

# 机器连不回来时的默认接法：客户端把 ssh 当传输层，relay 在 hub 上跑，一个端口都不开。
# token 经 stdin 写进 hub 的 0600 env 文件，不进 argv、不进 shell 历史。
install_stdio_over_ssh() {
  echo "mode: stdio over ssh (hub $HUB, relay $HUB_REPO/mcp_relay.py)"
  if [ "$MODE" = "dry-run" ]; then
    echo "+ ssh $HUB 'mkdir -p ~/.config/tmuxweb ~/.local/bin'"
    echo "+ write ~/.config/tmuxweb/relay.env (0600) on $HUB   # TMUXWEB_URL=$HUB_URL"
    echo "+ write ~/.local/bin/tmuxweb-relay-stdio (0755) on $HUB"
    printf '+ claude mcp add -s user %s -- /usr/bin/ssh %s <hub>/.local/bin/tmuxweb-relay-stdio\n' "$NAME" "$HUB"
    printf '+ codex mcp add %s -- /usr/bin/ssh %s <hub>/.local/bin/tmuxweb-relay-stdio\n' "$NAME" "$HUB"
    printf '+ hermes mcp add %s --command /usr/bin/ssh --args %s <hub>/.local/bin/tmuxweb-relay-stdio\n' "$NAME" "$HUB"
    echo "+ merge $NAME into $kimi_config as {\"command\": \"/usr/bin/ssh\", \"args\": [...]}"
    return 0
  fi

  remote_home=$(hub 'printf %s "$HOME"') || { echo "error: cannot ssh to $HUB" >&2; return 1; }
  remote_python=$(hub 'command -v python3 || true') || true
  [ -n "$remote_python" ] || { echo "error: no python3 on $HUB" >&2; return 1; }
  remote_repo=$(hub "cd $HUB_REPO 2>/dev/null && pwd") || true
  # 存在性必须在 hub 上判：本地没有这个路径
  { [ -n "$remote_repo" ] && hub "test -f '$remote_repo/mcp_relay.py'"; } || {
    echo "error: $HUB_REPO/mcp_relay.py not found on $HUB (use --hub-repo)" >&2; return 1; }

  wrapper="$remote_home/.local/bin/tmuxweb-relay-stdio"
  hub 'umask 077; mkdir -p "$HOME/.config/tmuxweb" "$HOME/.local/bin"'
  # env 文件：token 走 stdin，不落 argv
  { printf 'TMUXWEB_URL=%s\n' "$HUB_URL"; printf 'TMUXWEB_TOKEN=%s\n' "$TOKEN"; } \
    | hub 'cat > "$HOME/.config/tmuxweb/relay.env" && chmod 600 "$HOME/.config/tmuxweb/relay.env"'
  {
    printf '#!/bin/sh\n'
    printf '# TmuxWeb relay over ssh stdio —— 由 install_relay_mcp.sh 生成\n'
    printf 'set -a\n. "$HOME/.config/tmuxweb/relay.env"\nset +a\n'
    printf 'exec %s %s/mcp_relay.py "$@"\n' "$remote_python" "$remote_repo"
  } | hub "cat > '$wrapper' && chmod 750 '$wrapper'"
  echo "hub: wrapper installed at $wrapper"

  # 本机注册：command = ssh, args = [hub, wrapper]
  if have claude; then
    if claude mcp get "$NAME" >/dev/null 2>&1; then
      echo "claude: $NAME already registered"
    else
      run claude mcp add -s user "$NAME" -- "$SSH_BIN" "$HUB" "$wrapper"
    fi
  else echo "claude: not installed, skipping"; fi

  if have codex; then
    if codex mcp get "$NAME" >/dev/null 2>&1; then
      echo "codex: $NAME already registered"
    else
      run codex mcp add "$NAME" -- "$SSH_BIN" "$HUB" "$wrapper"
    fi
  else echo "codex: not installed, skipping"; fi

  if have hermes; then
    if hermes mcp list 2>/dev/null | grep -q "^$NAME\b"; then
      echo "hermes: $NAME already registered"
    else
      printf 'y\n' | hermes mcp add "$NAME" --command "$SSH_BIN" --args "$HUB" "$wrapper" \
        || echo "hermes: registration failed, see the message above"
    fi
  else echo "hermes: not installed, skipping"; fi

  if [ -d "${KIMI_CODE_HOME:-$HOME/.kimi-code}" ]; then
    "$PYTHON" - "$kimi_config" "$NAME" "$SSH_BIN" "$HUB" "$wrapper" <<'PY'
import json, sys
from pathlib import Path

path, name, ssh_bin, hub, wrapper = sys.argv[1:6]
file = Path(path)
data = {}
if file.exists():
    try:
        data = json.loads(file.read_text() or "{}")
    except json.JSONDecodeError:
        sys.exit("error: %s is not valid JSON, not touching it" % file)
data.setdefault("mcpServers", {})[name] = {"command": ssh_bin, "args": [hub, wrapper]}
file.parent.mkdir(parents=True, exist_ok=True)
file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
file.chmod(0o600)
print("kimi: %s written (%s, stdio over ssh)" % (name, file))
PY
  else echo "kimi: not found, skipping"; fi

  echo
  echo "verify:  hermes mcp test $NAME   (or: $SSH_BIN $HUB '$wrapper' < /dev/null)"
  echo "the tunnel/port is not needed: MCP speaks stdio over the ssh session."
}

case "$MODE" in
  http|stdio|auto|dry-run)
    if [ "$TRANSPORT" = "stdio-ssh" ]; then
      install_stdio_over_ssh
      if [ "$MODE" != "dry-run" ]; then
        for file in "$HOME/.codex/config.toml" "$HOME/.claude.json" "$kimi_config"; do
          [ -f "$file" ] && chmod 600 "$file" 2>/dev/null || true
        done
      fi
      exit 0
    fi
    if [ "$TRANSPORT" = "stdio" ]; then echo "mode: stdio ($PYTHON $SERVER)"; else echo "mode: http ($MCP_URL)"; fi
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

    # ---- Hermes: HTTP transport; the token goes into ~/.hermes/.env as
    #      MCP_<NAME>_API_KEY first, so `mcp add` never has to prompt for it
    if have hermes; then
      if hermes mcp list 2>/dev/null | grep -q "^$NAME\b"; then
        echo "hermes: $NAME already registered"
      elif [ "$MODE" = "dry-run" ]; then
        printf '+ hermes mcp add %s --url %s  (answers "requires auth?", then "enable all")\n' "$NAME" "$MCP_URL"
      elif [ "$MCP_AUTH" = "off" ]; then
        # no token to type in: answer "no authentication" to the prompt, then enable the tools
        printf 'n\ny\n' | hermes mcp add "$NAME" --url "$MCP_URL" \
          || echo "hermes: registration failed, see the message above"
      else
        HERMES_ENV="$HOME/.hermes/.env"
        HERMES_KEY="MCP_$(printf '%s' "$NAME" | tr '[:lower:]' '[:upper:]' | sed 's/[^A-Z0-9_]/_/g')_API_KEY"
        mkdir -p "$(dirname "$HERMES_ENV")"
        touch "$HERMES_ENV"
        chmod 600 "$HERMES_ENV"
        "$PYTHON" - "$HERMES_ENV" "$HERMES_KEY" "$TOKEN" <<'PY'
import pathlib, sys

path, key, token = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = [line for line in path.read_text().splitlines() if not line.startswith(key + "=")]
lines.append(f"{key}={token}")
path.write_text("\n".join(lines) + "\n")
PY
        printf 'y\ny\n' | hermes mcp add "$NAME" --url "$MCP_URL" --auth header \
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
    if [ -n "$HUB" ]; then
      "$SSH_BIN" "${SSH_OPTS[@]}" "$HUB" 'rm -f "$HOME/.local/bin/tmuxweb-relay-stdio" "$HOME/.config/tmuxweb/relay.env"; rmdir "$HOME/.config/tmuxweb" 2>/dev/null || true' \
        && echo "hub: wrapper removed from $HUB"
    fi
    echo "uninstalled $NAME"
    ;;

  *)
    echo "usage: $0 [--http|--stdio|--dry-run|--uninstall]" >&2
    exit 2
    ;;
esac
