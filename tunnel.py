"""Zero-config public URL: a cloudflared quick tunnel, started on demand.

Getting a phone onto this console from outside used to mean either a manual
certificate (`tailscale serve`), a Cloudflare account with its own tunnel config,
or port forwarding on a router. `cloudflared tunnel --url` needs none of that:
run it, read the random `https://<words>.trycloudflare.com` it prints, done.

What this module is careful about
    * it is opt-in: nothing runs until someone POSTs /api/tunnel/start, and the
      kill switch TMUX_WEB_TUNNEL=off refuses to start at all;
    * the URL is read from cloudflared's own output — we never guess a hostname;
    * the process is owned by this server: restart the service and the tunnel
      goes with it (no orphan process holding an old URL);
    * the console's own login still applies. This only changes reachability, not
      authentication — docs/tunnel.md says so in as many words.

`tailscale status` is reported too, because a tailnet address already needs no
configuration at all and is the better answer when the phone is on the tailnet.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

# 结尾必须真的是域名结束：`x.trycloudflare.com.attacker.net` 不算（(?![.\w-])）
URL_RE = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9-]*\.trycloudflare\.com(?![.\w-])")
DEFAULT_TIMEOUT = 25.0


class TunnelError(RuntimeError):
    pass


class QuickTunnel:
    def __init__(self, port: int, binary: str = "", target: str = "http://127.0.0.1",
                 timeout: float = DEFAULT_TIMEOUT):
        self.port = int(port)
        self.binary = binary or os.environ.get("TMUX_WEB_CLOUDFLARED") or "cloudflared"
        self.target = target
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._url = ""
        self._error = ""
        self._started = 0.0
        self._lock = threading.RLock()
        self._reader: threading.Thread | None = None

    # ---- 状态 -----------------------------------------------------------
    def enabled(self) -> bool:
        flag = (os.environ.get("TMUX_WEB_TUNNEL") or "on").strip().lower()
        return flag not in {"0", "off", "false", "no"}

    def binary_path(self) -> str:
        return shutil.which(self.binary) or ""

    def status(self) -> dict:
        with self._lock:
            running = bool(self._process and self._process.poll() is None)
            return {
                "enabled": self.enabled(),
                "binary": self.binary_path(),
                "running": running,
                "url": self._url if running else "",
                "started": self._started if running else 0,
                "error": self._error,
                "target": "%s:%d" % (self.target, self.port),
                "tailscale": _tailscale(),
            }

    # ---- 起停 -----------------------------------------------------------
    def start(self) -> dict:
        if not self.enabled():
            raise TunnelError("隧道被 TMUX_WEB_TUNNEL=off 关掉了")
        with self._lock:
            if self._process and self._process.poll() is None:
                return self.status()
            path = self.binary_path()
            if not path:
                raise TunnelError("没装 cloudflared：装上就能用，或在 TMUX_WEB_CLOUDFLARED 指定路径")
            self._url = ""
            self._error = ""
            try:
                self._process = subprocess.Popen(
                    [path, "tunnel", "--no-autoupdate", "--url",
                     "%s:%d" % (self.target, self.port)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
            except OSError as exc:
                raise TunnelError("cloudflared 起不来：%s" % exc) from exc
            self._started = time.time()
            self._reader = threading.Thread(target=self._drain, args=(self._process,),
                                            daemon=True)
            self._reader.start()
        # 等 URL 出现（cloudflared 先在 stderr 打一串日志，再给地址）
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            with self._lock:
                if self._url:
                    return self.status()
                if self._process and self._process.poll() is not None:
                    raise TunnelError(self._error or "cloudflared 退出了（看日志）")
            time.sleep(0.2)
        self.stop()
        raise TunnelError("等了 %.0f 秒没拿到公网地址" % self.timeout)

    def _drain(self, process: subprocess.Popen) -> None:
        """读 cloudflared 的输出：找 URL，其余留最后一行当错误线索。"""
        try:
            for line in process.stdout or []:
                text = line.strip()
                if not text:
                    continue
                found = URL_RE.search(text)
                if found:
                    with self._lock:
                        self._url = found.group(0)
                    continue
                if "ERR" in text or "error" in text.lower():
                    with self._lock:
                        self._error = text[:300]
        except (OSError, ValueError):
            pass

    def stop(self) -> dict:
        with self._lock:
            process, self._process = self._process, None
            self._url = ""
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return self.status()


def _tailscale() -> dict:
    """tailnet 地址：本来就不需要任何配置，能连就直接连它。"""
    path = shutil.which("tailscale")
    if not path:
        return {"installed": False, "running": False, "addresses": []}
    try:
        completed = subprocess.run([path, "status", "--json"], capture_output=True,
                                   text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"installed": True, "running": False, "addresses": []}
    if completed.returncode != 0:
        return {"installed": True, "running": False, "addresses": []}
    # 只取**本机**地址（Self.TailscaleIPs）。Peer 那一堆是同网段别的机器，
    # 列出来会让人以为可以从那些地址连进来 —— 那是别人的机器。
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        return {"installed": True, "running": False, "addresses": []}
    self_info = payload.get("Self") or {}
    addresses = [item for item in (self_info.get("TailscaleIPs") or [])
                 if isinstance(item, str) and item.startswith("100.")]
    return {"installed": True, "running": bool(payload.get("BackendState") == "Running"),
            "addresses": addresses, "hostname": self_info.get("HostName") or ""}
