#!/usr/bin/env python3
"""自测：下载链接到底落在 MagicDNS 上，还是又绕回了 Cloudflare。

跑一次就能回答"我点下载，浏览器拿到的那条 URL 是哪条线路"——不用手动去浏览器里试。

做四件事：

1. 读 `/api/system/addresses`，按前端同样的规则挑出"能当下载源站"的条目
   （https + tailnet，也就是 `https://<name>.ts.net` 这种 MagicDNS 名字），
   打印每一条以及它能不能用。
2. 挑一条能用的直连源站（默认就是 MagicDNS），签一张下载票据。
3. 真的把字节从那条源站拉回来，校验大小和内容 —— 通不通不是靠"打了个 200"猜。
4. 同样给打包下载签一张 zip 票据，拉回来校验是合法 zip。

用法（在仓库根目录）：

    ./.venv/bin/python scripts/selfcheck_download.py
    ./.venv/bin/python scripts/selfcheck_download.py --json     # 给 CI / 脚本用

退出码：0 全部通过；1 有检查失败；2 环境不对（没登录 / 没有可用直连源站）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("TMUX_WEB_BASE", "http://127.0.0.1:8787").rstrip("/")


def api_token() -> str:
    token = os.environ.get("TMUX_WEB_API_TOKEN", "").strip()
    if token:
        return token
    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            if line.startswith("TMUX_WEB_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


TOKEN = api_token()


def request(method: str, url: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-TmuxWeb-Token": TOKEN}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def json_request(method: str, path: str, body: dict | None = None):
    status, payload, _ = request(method, BASE + path, body)
    if status != 200:
        raise SystemExit(f"环境不对：{method} {path} -> {status} {payload[:200]!r}")
    return json.loads(payload)


def transfer_origins() -> list[dict]:
    """前端 `origins()` 的同一套规则：https、标注 transfer、不是当前页面。"""
    data = json_request("GET", "/api/system/addresses")
    return [item for item in data.get("addresses") or [] if item.get("transfer")]


def is_magicdns(url: str) -> bool:
    """`https://name.tailnet.ts.net`：tailnet 的 MagicDNS 名字，不是裸 IP。"""
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    return host.endswith(".ts.net") and not re.match(r"^100\.", host)


def pick_origin(entries: list[dict]) -> dict | None:
    """MagicDNS 名字排第一；没有就退到其它能当源站的 https 地址。"""
    magic = [item for item in entries if is_magicdns(item["url"])]
    tailnet = [item for item in entries if item.get("kind") == "tailnet"]
    for group in (magic, tailnet, entries):
        if group:
            return group[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = parser.parse_args()

    if not TOKEN:
        print("缺少 TMUX_WEB_API_TOKEN（环境变量或 .env）", file=sys.stderr)
        return 2

    report: dict = {"base": BASE, "checks": [], "ok": True}

    def check(name: str, ok: bool, detail: str) -> bool:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        report["ok"] = report["ok"] and ok
        if not args.json:
            print(("  ✓ " if ok else "  ✗ ") + name + " — " + detail)
        return ok

    entries = transfer_origins()
    if not args.json:
        print(f"控制台：{BASE}")
        print("可用作下载源站：")
        for item in entries:
            print(f"  {item.get('kind','?'):8} {item['url']}"
                  + ("   ← MagicDNS" if is_magicdns(item["url"]) else ""))
        if not entries:
            print("  （一条都没有：只能在页面自己的域名上转，改不了道）")

    origin = pick_origin(entries)
    if origin is None:
        check("有可用的直连源站", False, "没有 https 的直连地址（tailscale serve 或 TMUX_WEB_TL… 没配）")
        print(json.dumps(report, ensure_ascii=False) if args.json else "")
        return 2

    report["origin"] = origin["url"]
    check("源站是 MagicDNS 名字", is_magicdns(origin["url"]), origin["url"])

    # 浏览器为什么可能不理这条源站：账号偏好被设成 off，或者页面根本没加载到新代码
    prefs = json_request("GET", "/api/prefs").get("prefs") or {}
    check("账号偏好没把下载压在 CF 上", prefs.get("transfer") != "off",
          f"transfer={prefs.get('transfer','(未设置)')}")
    status, bundle, _ = request("GET", BASE + "/assets/console.bundle.js")
    text = bundle.decode(errors="replace")
    check("页面加载的 bundle 里有线路逻辑",
          status == 200 and "tmuxweb.transfer.route" in text and "syncPrefsOnce" in text,
          f"{status} · {len(bundle)} 字节")

    # 找一个真实 pane，写一个临时文件当下载目标
    panes = json_request("GET", "/api/panes").get("panes") or []
    if not panes:
        check("有可用的 pane", False, "没有任何 pane")
        print(json.dumps(report, ensure_ascii=False) if args.json else "")
        return 2
    pane = panes[0]
    marker = uuid.uuid4().hex
    target = f"/tmp/tmuxweb-selfcheck-{marker}.txt"
    status, out, _ = request("POST", BASE + "/api/exec",
                             {"host": "localhost",
                              "command": f"printf '%s' '{marker}' > {target}", "timeout": 30})
    try:
        ran = json.loads(out) if status == 200 else {}
    except ValueError:
        ran = {}
    if ran.get("exit") != 0:
        check("写入临时文件", False, f"exec -> {status} {out[:160]!r}")
        print(json.dumps(report, ensure_ascii=False) if args.json else "")
        return 2

    # —— 单文件下载 ——
    grant = json_request("POST", "/api/transfer/ticket",
                         {"pane": pane["id"], "path": target, "direction": "down"})
    direct_url = origin["url"] + "/t/download?t=" + urllib.parse.quote(grant["token"], safe="")
    report["download_url"] = direct_url.split("?")[0] + "?t=***"
    report["download_host"] = re.sub(r"^https?://", "", direct_url).split("/")[0]
    check("下载链接指向 MagicDNS", is_magicdns(direct_url), report["download_host"])
    status, payload, headers = request("GET", direct_url)
    size_ok = len(payload) == len(marker) and payload.decode(errors="replace") == marker
    check("直连取回的字节正确", status == 200 and size_ok,
          f"{status} · {len(payload)} 字节")

    # —— 打包下载 ——
    grant = json_request("POST", "/api/transfer/ticket",
                         {"pane": pane["id"], "path": os.path.dirname(target),
                          "direction": "zip", "paths": [target]})
    zip_url = origin["url"] + "/t/zip?t=" + urllib.parse.quote(grant["token"], safe="")
    report["zip_url"] = zip_url.split("?")[0] + "?t=***"
    status, blob, headers = request("GET", zip_url, timeout=120)
    check("打包下载走 MagicDNS", is_magicdns(zip_url) and status == 200 and blob[:2] == b"PK",
          f"{status} · {len(blob)} 字节 · {'zip' if blob[:2] == b'PK' else '不是 zip'}")

    request("POST", BASE + "/api/exec",
            {"host": "localhost", "command": f"rm -f {target}", "timeout": 20})

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print("\n结论：" + ("下载走 MagicDNS，一切正常" if report["ok"] else "有检查没过"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
