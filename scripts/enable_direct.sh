#!/usr/bin/env bash
# 给局域网 / Tailscale 里访问的人一条「不经过 Cloudflare」的入口。
#
# 为什么需要：CF 隧道套住的站点，单请求 100MB、单请求 100 秒，大文件传不动。
# 同一网段或同一个 tailnet 里的人直连服务器 IP 就完全没有这些限制，而且文件
# 不离开内网。
#
# 用法：
#   ./scripts/enable_direct.sh          # 看当前状态和建议
#   ./scripts/enable_direct.sh serve    # 真的起 tailscale serve（需要 sudo 一次）
#   ./scripts/enable_direct.sh off      # 关掉
set -u

PORT="${TMUX_WEB_PORT:-8787}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "没有装 tailscale。局域网内直接访问 http://<本机IP>:$PORT 就行。"
  exit 0
fi

LAN_IPS=$(ip -4 -o addr show scope global 2>/dev/null \
  | awk '$2 !~ /^(docker|br-|veth|virbr|tun|tap|tailscale)/ {split($4,a,"/"); print a[1]}')
TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
DNS_NAME=$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)
SERVE=$(tailscale serve status 2>/dev/null | head -1)

echo "局域网："
for ip in $LAN_IPS; do echo "  http://$ip:$PORT"; done
echo "Tailscale："
[ -n "$TS_IP" ] && echo "  http://$TS_IP:$PORT"
[ -n "$DNS_NAME" ] && echo "  https://$DNS_NAME   （需要先 tailscale serve）"
echo
echo "当前 serve 状态：${SERVE:-无}"
echo

case "${1:-status}" in
  serve)
    if ! tailscale serve status >/dev/null 2>&1; then
      echo "先给当前用户 operator 权限（只需一次）："
      echo "  sudo tailscale set --operator=$USER"
      echo "然后在 tailnet 管理台开 HTTPS 证书（DNS 页面 → Enable HTTPS）。"
      exit 1
    fi
    tailscale serve --bg "$PORT"
    echo
    echo "好了，用 https://$DNS_NAME 访问（只在 tailnet 内可达，真证书，无混合内容问题）。"
    echo "控制台的 设置 › 直连地址 里会自动多出这一条。"
    ;;
  off)
    tailscale serve reset
    echo "已关闭 serve。"
    ;;
  *)
    echo "想让 tailnet 里用 https://$DNS_NAME 访问："
    echo "  ./scripts/enable_direct.sh serve"
    ;;
esac
