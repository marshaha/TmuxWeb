#!/usr/bin/env bash
# Keep a reverse SSH tunnel open so a machine that cannot reach this host
# (no tailnet, behind NAT, outbound-only) can still reach the TmuxWeb MCP
# endpoint through its own 127.0.0.1.
#
#   ./scripts/reverse_tunnel.sh marscrazy@100.123.123.12
#   ./scripts/reverse_tunnel.sh marscrazy@host --remote-port 8787 --local-port 8787
#   ./scripts/reverse_tunnel.sh marscrazy@host --print-systemd   # unit for the *remote*
#   ./scripts/reverse_tunnel.sh marscrazy@host --print-launchd   # plist for the *remote* (macOS)
#
# Then, on the far side, the MCP client points at http://127.0.0.1:8787/mcp.
set -euo pipefail

TARGET=""
SSH_PORT=22
LOCAL_PORT=8787
REMOTE_PORT=8787
BIND=127.0.0.1          # 127.0.0.1 keeps the port off the remote's network; needs no GatewayPorts
IDENTITY=""
ACTION="run"

usage() {
  sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'

options:
  --ssh-port N      SSH port on the remote (default 22)
  --local-port N    TmuxWeb port on this host (default 8787)
  --remote-port N   port to open on the remote's 127.0.0.1 (default 8787)
  --bind ADDR       remote listen address (default 127.0.0.1; 0.0.0.0 needs GatewayPorts)
  --identity FILE   SSH private key
  --print-systemd   print a systemd unit that runs this tunnel, then exit
  --print-launchd   print a launchd plist (macOS), then exit
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --local-port) LOCAL_PORT="$2"; shift 2 ;;
    --remote-port) REMOTE_PORT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --print-systemd) ACTION="systemd"; shift ;;
    --print-launchd) ACTION="launchd"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) TARGET="$1"; shift ;;
  esac
done

if [ -z "$TARGET" ]; then
  echo "missing target, e.g. ./scripts/reverse_tunnel.sh user@host" >&2
  exit 2
fi

FORWARD="$REMOTE_PORT:$BIND:$LOCAL_PORT"
# Where the client actually connects to, printed at the end of a successful start
CLIENT_URL="http://$BIND:$REMOTE_PORT/mcp"

SSH_CPU_OPTS=(
  -N -T
  -o ExitOnForwardFailure=yes      # 隧道起不来就退出，别假装连着
  -o ServerAliveInterval=20        # 掉线 60 秒内发现
  -o ServerAliveCountMax=3
  -o TCPKeepAlive=yes
  -o BatchMode=yes                 # 用 key，不交互
  -o StrictHostKeyChecking=accept-new
)
[ -n "$IDENTITY" ] && SSH_CPU_OPTS+=(-i "$IDENTITY")
SSH_CPU_OPTS+=(-p "$SSH_PORT" -R "$FORWARD")

if [ "$ACTION" = "systemd" ]; then
  cat <<EOF
# 拨号方（能连到远端的那台机器，通常就是跑 TmuxWeb 的这台）：
# ~/.config/systemd/user/tmuxweb-reverse-tunnel.service
# 前提：本机的公钥已在远端的 authorized_keys 里（这里是本机主动 dial 远端）。
[Unit]
Description=TmuxWeb reverse tunnel (127.0.0.1:$REMOTE_PORT -> localhost:$LOCAL_PORT)
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 \\
    -o ServerAliveCountMax=3 -o TCPKeepAlive=yes -p $SSH_PORT -R $FORWARD $TARGET
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  exit 0
fi

if [ "$ACTION" = "launchd" ]; then
  cat <<EOF
<!-- 远端 macOS：~/Library/LaunchAgents/com.tmuxweb.reverse-tunnel.plist
     加载：launchctl load -w ~/Library/LaunchAgents/com.tmuxweb.reverse-tunnel.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tmuxweb.reverse-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string><string>-T</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=20</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>TCPKeepAlive=yes</string>
    <string>-p</string><string>$SSH_PORT</string>
    <string>-R</string><string>$FORWARD</string>
    <string>$TARGET</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/tmp/tmuxweb-reverse-tunnel.log</string>
  <key>StandardErrorPath</key><string>/tmp/tmuxweb-reverse-tunnel.log</string>
</dict>
</plist>
EOF
  exit 0
fi

log() { printf '%s reverse-tunnel: %s\n' "$(date '+%F %T')" "$*"; }

if command -v autossh >/dev/null 2>&1; then
  # autossh 自己会重连；-M 0 关掉它的监控端口（靠 ssh 的 ServerAlive 就够）
  log "using autossh: -R $FORWARD -> $TARGET:$SSH_PORT"
  exec autossh -M 0 "${SSH_CPU_OPTS[@]}" "$TARGET"
fi

backoff=1
while :; do
  log "connecting: -R $FORWARD -> $TARGET:$SSH_PORT"
  started=$(date +%s)
  if ssh "${SSH_CPU_OPTS[@]}" "$TARGET"; then
    log "tunnel closed cleanly"
    exit 0
  fi
  code=$?
  ran=$(( $(date +%s) - started ))
  if [ "$code" = "255" ] && [ "$ran" -gt 30 ]; then
    backoff=1                        # 连上过一阵再断：立刻重试
  fi
  log "tunnel exited (code $code after ${ran}s); retry in ${backoff}s"
  sleep "$backoff"
  backoff=$(( backoff * 2 > 30 ? 30 : backoff * 2 ))
done
