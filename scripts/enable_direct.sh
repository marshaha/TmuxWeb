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
    # 注意：不能用 $(...) 捕获输出 —— 那种形式下 tailscale serve 会一直不返回（实测卡死）。
    # 重定向到临时文件、再 cat 出来，是唯一稳定的写法。
    LOG=$(mktemp)
    # tailscale serve 在 tailnet 没开 Serve 时会把提示打印完就不退出（实测一直挂着），
    # 所以这里自己加超时，把已经打印出来的提示照原样交给用户。
    timeout 20 tailscale serve --bg "$PORT" </dev/null >"$LOG" 2>&1
    STATUS=$?
    cat "$LOG"
    OUTPUT=$(cat "$LOG")
    rm -f "$LOG"
    if echo "$OUTPUT" | grep -q "Serve is not enabled"; then
      echo
      echo "tailnet 还没开 Serve。用上面那个链接开一次（管理员账号，点一下就行），"
      echo "然后重新跑：./scripts/enable_direct.sh serve"
      exit 2
    fi
    if [ $STATUS -eq 124 ] && [ -s /dev/null ]; then
      echo
      echo "tailscale serve 没有返回（tailnet 可能还没开 Serve；或者本用户没有 operator 权限）。"
      echo "管理员开关：https://login.tailscale.com/admin/settings/features → Serve"
      exit 2
    fi
    if [ $STATUS -ne 0 ]; then
      case "$OUTPUT" in
        *permission*|*operator*|*denied*)
          echo
          echo "没有权限改 serve 配置。先给当前用户 operator（只需一次，要密码）："
          echo "  sudo tailscale set --operator=$USER"
          ;;
      esac
      exit $STATUS
    fi
    echo
    echo "好了，用 https://$DNS_NAME 访问（只在 tailnet 内可达，真证书，无混合内容问题）。"
    echo "控制台的 设置 › 直连地址 里会自动多出这一条。"
    echo "如果要跨网段公开给外网，再配 TMUX_WEB_TRANSFER_ORIGINS。"
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
