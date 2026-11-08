#!/usr/bin/env bash
# Where can this machine reach TmuxWeb?
#
#   ./scripts/relay-endpoint.sh           # list candidates, one "kind<TAB>url" per line
#   ./scripts/relay-endpoint.sh --probe   # print the first one that actually answers, exit 1 if none
#   ./scripts/relay-endpoint.sh --all     # list with "ok"/"no" for each
#
# The criterion is not "is it Tailscale": any private network counts — EasyTier,
# ZeroTier, WireGuard, a subnet router, or a plain LAN segment. What matters is
# whether a bidirectional private L3 path to port 8787 exists. Hosts that can
# only be reached *through a jump host* are the exception (they get stdio over
# ssh instead — see install_relay_mcp.sh --hub).
set -uo pipefail

PORT="${TMUX_WEB_PORT:-8787}"
MODE="${1:---list}"
CANDIDATES=""          # kind<TAB>url lines, best first

add() {  # $1=kind $2=host
  [ -n "${2:-}" ] || return 0
  case "$2" in
    *:*) url="http://[$2]:$PORT" ;;          # IPv6
    *)   url="http://$2:$PORT" ;;
  esac
  case "
$CANDIDATES" in
    *"	$url"*) return 0 ;;                 # dedupe
  esac
  CANDIDATES="$CANDIDATES$1	$url
"
}

first_ip() {  # $1=interface
  ip -4 -o addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1
}

# ---- 1. explicit override wins
if [ -n "${TMUXWEB_URL:-}" ]; then
  CANDIDATES="explicit	${TMUXWEB_URL%/}
"
else
  # ---- 2. overlay networks (the portable ones): tailscale / easytier / zerotier / wireguard
  for bin in tailscale /usr/bin/tailscale /opt/homebrew/bin/tailscale \
             /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    if command -v "$bin" >/dev/null 2>&1; then
      add tailscale "$("$bin" ip -4 2>/dev/null | head -1)"
      break
    fi
  done

  # EasyTier: the CLI knows its own virtual IP; fall back to the interface
  if command -v easytier-cli >/dev/null 2>&1; then
    add easytier "$(easytier-cli node 2>/dev/null | sed -n 's/.*ipv4[^0-9]*\([0-9.]\+\).*/\1/p' | head -1)"
  fi
  for iface in $(ip -4 -o addr show 2>/dev/null | awk '$2!="lo"{print $2}' | sed 's/@.*//' | grep -E '^(easytier|et|zt)' | sort -u); do
    add overlay "$(first_ip "$iface")"
  done

  if command -v zerotier-cli >/dev/null 2>&1; then
    for iface in $(zerotier-cli listnetworks 2>/dev/null | awk 'NR>1{print $8}' | grep -E '^zt'); do
      add zerotier "$(first_ip "$iface")"
    done
  fi
  for iface in $(ip -4 -o addr show 2>/dev/null | awk '$2!="lo"{print $2}' | sed 's/@.*//' | grep -E '^wg[0-9]+$' | sort -u); do
    add wireguard "$(first_ip "$iface")"
  done

  # ---- 3. the physical LAN this machine sits on (default route)
  lan_if=""
  if command -v ip >/dev/null 2>&1; then
    lan_if=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  elif command -v route >/dev/null 2>&1; then
    lan_if=$(route -n get default 2>/dev/null | sed -n 's/.*interface: *//p' | head -1)
  fi
  [ -n "$lan_if" ] && add lan "$(first_ip "$lan_if")"

  # ---- 4. loopback last: always works *on* the hub, useless anywhere else
  add localhost 127.0.0.1
fi

probe() {  # $1=url
  command -v curl >/dev/null 2>&1 || return 0
  # --noproxy：一定要测「直连」。有代理的机器上，代理会把别的网段答成 200/502，
  # 把真正可达的地址（比如 tailnet）掩盖掉，也会让不可达的地址看起来可达。
  code=$(curl -s --noproxy '*' -m 4 -o /dev/null -w '%{http_code}' "${1%/}/" 2>/dev/null || true)
  case "$code" in
    200|204|301|302|303|307|308|401|403) return 0 ;;
    *) return 1 ;;
  esac
}

case "$MODE" in
  --list|list|"")
    printf '%s' "$CANDIDATES"
    ;;
  --all)
    printf '%s' "$CANDIDATES" | while IFS='	' read -r kind url; do
      [ -n "$url" ] || continue
      if probe "$url"; then printf '%s\t%s\tok\n' "$kind" "$url"; else printf '%s\t%s\tno\n' "$kind" "$url"; fi
    done
    ;;
  --probe)
    first=""
    while IFS='	' read -r kind url; do
      [ -n "$url" ] || continue
      [ -n "$first" ] || first="$url"
      if probe "$url"; then printf '%s\n' "$url"; exit 0; fi
    done <<< "$CANDIDATES"
    # 没有能通的：把首选地址打在 stderr 方便日志里看，退出码 1 交给调用方决定
    [ -n "$first" ] && printf '%s\n' "$first" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 [--list|--probe|--all]" >&2
    exit 2
    ;;
esac
