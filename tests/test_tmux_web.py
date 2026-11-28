import asyncio
import hashlib
import hmac
import io
import os
import secrets
import threading
import time
import re
import shutil
import subprocess
import unittest
from unittest import mock
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from fastapi.testclient import TestClient
from pydantic import ValidationError

import tmux_web
import agent_catalog
from tmux_web import ANSI_RE, TmuxBackend, TmuxError, create_app, ensure_share_schema
import relay
from tests.fake_terminal import FakeTerminalFactory
from remote_hosts import host_key_fingerprint, normalize_session_name, pane_ref


class FakeTmux(TmuxBackend):
    def __init__(self):
        self.panes = [
            {
                "id": "%4",
                "target": "codex:0.0",
                "session": "codex",
                "window": "0",
                "window_name": "editor",
                "index": "0",
                "command": "codex",
                "title": "Review | repo",
                "path": "/work/repo",
                "active": True,
            }
        ]
        self.output = "hello\n› Ask Codex to do anything\n"
        self.sent = []
        self.keys = []
        self.created = []
        self.auto_reply = None

    def create_session(self, name, path="", command=None, create_dir=True, keep_open=True):
        try:
            name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        if any(pane["session"] == name for pane in self.panes):
            raise TmuxError("session already exists: %s" % name)
        made_directory = False
        if path and not Path(path).is_dir():
            if not create_dir:
                raise TmuxError("no such directory: %s" % path)
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TmuxError("cannot create directory %s: %s" % (path, exc)) from exc
            made_directory = True
        self.created.append((name, path))
        self.created_commands = getattr(self, "created_commands", [])
        self.created_commands.append((name, list(command) if command else []))
        self.created_shell = getattr(self, "created_shell", [])
        self.created_shell.append(agent_catalog.with_keep_open(command) if (command and keep_open)
                                  else (agent_catalog.render(command) if command else ""))
        pane = {"id": "%12", "pane_id": "%12", "target": "%s:0.0" % name, "session": name,
                "window": "0", "window_name": "shell", "index": "0", "command": "zsh",
                "title": "new session", "path": path or "/work/repo", "active": True}
        self.panes.append(pane)
        return {"session": name, "path": path, "created_dir": made_directory,
                "command": agent_catalog.render(command) if command else ""}

    def kill_pane(self, pane_id):
        pane = next((item for item in self.panes if item["id"] == pane_id), None)
        if pane is None:
            raise TmuxError("no such pane: %s" % pane_id)
        session = pane["session"]
        self.panes = [item for item in self.panes if item["id"] != pane_id]
        alive = any(item["session"] == session for item in self.panes)
        return {"pane": pane_id, "session": session, "session_alive": alive}

    def rename_session(self, session, name):
        try:
            new_name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        current = session
        if str(session).startswith("%"):
            current = next(p["session"] for p in self.panes if p["id"] == session)
        self.renamed_sessions = getattr(self, "renamed_sessions", [])
        self.renamed_sessions.append((current, new_name))
        for pane in self.panes:
            if pane["session"] == current:
                pane["session"] = new_name
        return {"session": new_name, "old": current}

    def rename_window(self, pane_id, name):
        if not any(pane["id"] == pane_id for pane in self.panes):
            raise TmuxError("no such pane: %s" % pane_id)
        self.renamed_windows = getattr(self, "renamed_windows", [])
        self.renamed_windows.append((pane_id, name))
        return {"pane": pane_id, "window": name}

    def set_pane_name(self, pane_id, name):
        self.named_panes = getattr(self, "named_panes", [])
        self.named_panes.append((pane_id, name))
        for pane in self.panes:
            if pane["id"] == pane_id:
                if name:
                    pane["custom_name"] = name
                else:
                    pane.pop("custom_name", None)
                return {"pane": pane_id, "name": name}
        raise TmuxError("no such pane: %s" % pane_id)

    def kill_session(self, name):
        try:
            name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        if not any(pane["session"] == name for pane in self.panes):
            raise TmuxError("no such session: %s" % name)
        self.panes = [pane for pane in self.panes if pane["session"] != name]
        self.killed = getattr(self, "killed", [])
        self.killed.append(name)
        return {"session": name, "killed": True}

    def list_panes(self):
        return self.panes

    def send_key(self, pane_id, key, pane=None):
        """记录按键，绝不落到真实 tmux 上。

        继承来的 TmuxBackend.send_key 会真的执行 `tmux send-keys`：本机恰好有
        pane %4 时测试会"通过"，CI 上没有 tmux 就 503 —— 更糟的是本地那一跑会
        往用户真实 pane 里敲按键。用例可以覆盖这个方法，别覆盖回父类。
        """
        self.validate_pane(pane_id)
        self.keys.append((pane_id, key))

    def send_keys(self, pane_id, keys, pane=None):
        """批量发送在测试里逐条走 send_key，用例只要拦一个方法就够。"""
        for key in keys:
            self.send_key(pane_id, key, pane=pane)

    def capture(self, pane_id, history=500, pane=None, visible_only=False):
        self.validate_pane(pane_id)
        return self.output

    def capture_ansi(self, pane_id, history=500, pane=None, visible_only=False):
        self.validate_pane(pane_id)
        return "\x1b[32mhello\x1b[0m\n› Ask Codex to do anything\n"

    def send(self, pane_id, text, submit=True, pane=None):
        self.validate_pane(pane_id)
        self.sent.append((pane_id, text, submit))
        if self.auto_reply:
            match = re.search(r"TMUXWEB_REPLY_([0-9a-f]{24}):", text)
            if match:
                self.output = "hello\n" + match.group(0) + ' "' + self.auto_reply + '"\n'

    def transcript(self, pane_id, limit=200, refresh=False, pane=None):
        self.validate_pane(pane_id)
        if self.panes[0]["command"] != "claude":
            raise RuntimeError("not claude")
        return {
            "pane": pane_id,
            "session_id": "session-1",
            "mtime": 1,
            "messages": [
                {"id": "u1", "role": "user", "text": "问题", "timestamp": None},
                {"id": "a1", "role": "assistant", "text": "| A | B |\n|---|---|\n| 1 | 2 |", "timestamp": None},
            ],
        }


class FakeRemoteStore:
    def __init__(self):
        self.hosts = {}
        self._sequence = 0
        self.agent_tokens = {}      # host_id -> {"token_hash", "created_at", "last_seen"}

    def add_host(self, **data):
        self._sequence += 1
        host = {"id": "0123456789abc%03d" % self._sequence, "enabled": True, "created_at": 0.0,
                "position": len(self.hosts), **data}
        host.setdefault("transport", "ssh")
        credential = host.pop("credential")
        self.hosts[host["id"]] = (host, credential)
        return host
    def list_hosts(self): return [item[0] for item in self.hosts.values()]
    def get_host(self, host_id, include_credential=False):
        host, credential = self.hosts[host_id]
        return {**host, **({"credential": credential} if include_credential else {})}
    def delete_host(self, host_id): return self.hosts.pop(host_id, None) is not None

    def update_host(self, host_id, **changes):
        host, credential = self.hosts[host_id]
        updated = dict(host)
        for key in ("name", "hostname", "port", "username", "auth_type", "host_key", "fingerprint",
                    "jump_host_id", "transport"):
            if changes.get(key) is not None:
                updated[key] = changes[key]
        if updated.get("transport") not in (None, "ssh", "agent"):
            raise ValueError("transport must be 'ssh' or 'agent'")
        if changes.get("credential") is not None:
            credential = changes["credential"]
        if updated["auth_type"] == "agent":
            credential = ""
        # validate before storing, so a rejected edit changes nothing
        if updated["auth_type"] in {"password", "key"} and not credential:
            raise ValueError("password or private key is required")
        self.hosts[host_id] = (updated, credential)
        return dict(updated)

    def jump_chain(self, host, limit=5):
        chain, seen = [], set()
        current = (host.get("jump_host_id") or "").strip()
        while current and len(chain) < limit and current not in seen:
            seen.add(current)
            try:
                hop = self.get_host(current, include_credential=True)
            except KeyError:
                break
            chain.append(hop)
            current = (hop.get("jump_host_id") or "").strip()
        return chain

    def reorder_hosts(self, ordered_ids):
        known = list(self.hosts)
        if sorted(ordered_ids) != sorted(known):
            raise ValueError("the reorder list must contain every host exactly once")
        self.hosts = {host_id: self.hosts[host_id] for host_id in ordered_ids}
        for position, host_id in enumerate(ordered_ids):
            self.hosts[host_id][0]["position"] = position
        return self.list_hosts()

    # -- agent tokens (same shape as RemoteHostStore: only the sha256 is kept) --
    def issue_agent_token(self, host_id):
        if host_id not in self.hosts:
            raise KeyError(host_id)
        token = secrets.token_urlsafe(32)
        self.agent_tokens[host_id] = {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": time.time(), "last_seen": None}
        return token

    def verify_agent_token(self, token):
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for host_id, record in self.agent_tokens.items():
            if hmac.compare_digest(record["token_hash"], digest):
                return host_id
        return None

    def revoke_agent_token(self, host_id):
        return self.agent_tokens.pop(host_id, None) is not None

    def agent_token_info(self, host_id):
        record = self.agent_tokens.get(host_id)
        if record is None:
            return None
        return {"created_at": record["created_at"], "last_seen": record["last_seen"]}

    def touch_agent_last_seen(self, host_id, at):
        if host_id in self.agent_tokens:
            self.agent_tokens[host_id]["last_seen"] = at


class FakeSSHRunner:
    sent = []
    screen = "\x1b[32mREMOTE_OK\x1b[0m\n❯ "
    auto_reply = None
    run_calls = []          # (command, input_bytes) for every run()
    upload_calls = []       # (path, data) for every upload_file()

    def __init__(self, host):
        self.host = host
        self.created = []

    def run(self, remote_command, *, input_bytes=None, timeout=15, resolve_tmux=True):
        """Record the call and answer like a healthy host (no real ssh)."""
        FakeSSHRunner.run_calls.append((remote_command, input_bytes))
        if remote_command.startswith("printf"):
            return subprocess.CompletedProcess(args=remote_command, returncode=0,
                                               stdout=b"/home/fake", stderr=b"")
        return subprocess.CompletedProcess(args=remote_command, returncode=0,
                                           stdout=b"", stderr=b"")

    def upload_file(self, path, data):
        FakeSSHRunner.upload_calls.append((path, data))
    def check(self, timeout=12):
        FakeSSHRunner.last_check_timeout = timeout
        return "TMUX_REMOTE_OK"
    def create_session(self, name, path="", command=None, create_dir=True, keep_open=True):
        name = normalize_session_name(name)
        self.created.append((name, path))
        FakeSSHRunner.created = (name, path)
        FakeSSHRunner.created_command = list(command) if command else []
        FakeSSHRunner.created_create_dir = create_dir
        FakeSSHRunner.created_keep_open = keep_open
        return {"session": name, "path": path, "created_dir": create_dir and bool(path),
                "command": " ".join(command or [])}

    def kill_pane(self, pane_id):
        FakeSSHRunner.killed_pane = pane_id
        return {"pane": pane_id, "session": "remote", "session_alive": True}

    def rename_session(self, session, name):
        FakeSSHRunner.renamed = (session, name)
        return {"session": name, "old": session}

    def rename_window(self, pane_id, name):
        FakeSSHRunner.renamed_window = (pane_id, name)
    def set_pane_name(self, pane_id, name):
        FakeSSHRunner.named_pane = (pane_id, name)
        return {"pane": pane_id, "name": name}
        return {"pane": pane_id, "window": name}

    def kill_session(self, name):
        FakeSSHRunner.killed = name
        return {"session": name, "killed": True}

    def probe_agents(self, timeout=12):
        return [dict(item) for item in getattr(self, "agents", [])]
    def ping(self, timeout=3):
        """The console's liveness handshake: a healthy fake host answers."""
        FakeSSHRunner.last_ping_timeout = timeout
    def list_panes(self, timeout=15):
        FakeSSHRunner.last_list_timeout = timeout
        extra = [{"id": pane_ref(self.host["id"], "%11"), "pane_id": "%11", "host_id": self.host["id"],
                  "target": "%s:0.0" % name, "session": name, "window": "0", "window_name": "shell",
                  "index": "0", "command": "zsh", "title": "new", "path": path or "/srv/work",
                  "active": True, "width": 100, "height": 30}
                 for name, path in getattr(self, "created", [])]
        return extra + [{"id": pane_ref(self.host["id"], "%9"), "pane_id": "%9", "host_id": self.host["id"],
                 "target": "remote:0.0", "session": "remote", "window": "0", "window_name": "agent",
                 "index": "0", "command": "claude", "title": "Remote Claude", "path": "/srv/work",
                 "active": True, "width": 100, "height": 30}]
    def capture_ansi(self, pane_id, history=5000, pane=None, visible_only=False): return FakeSSHRunner.screen
    def capture(self, pane_id, history=5000, pane=None, visible_only=False):
        return ANSI_RE.sub("", FakeSSHRunner.screen)
    def snapshot_ansi(self, pane_id, history=5000, known_digest=None, pane=None):
        content = self.capture_ansi(pane_id, history)
        digest = hashlib.sha1(content.encode()).hexdigest()
        return digest, (None if digest == known_digest else content)
    def transcript(self, pane_id, limit=200, refresh=False, pane=None):
        return {"pane": pane_id, "provider": "claude", "session_id": "remote-session-1", "mtime": 7,
                "messages": [{"id": "r1", "role": "user", "text": "远端问题", "timestamp": None}]}
    remote_files = {}      # path -> bytes，测远端文件预览用

    def file_size(self, path):
        data = FakeSSHRunner.remote_files.get(path)
        return len(data) if data is not None else None

    def stream_file(self, path, max_bytes, chunk=65536, idle_timeout=90.0):
        data = FakeSSHRunner.remote_files.get(path)
        if data is None:
            raise RuntimeError("no such file: %s" % path)
        for start in range(0, len(data), chunk):
            yield data[start:start + chunk]

    def stream_range(self, path, start, length, chunk=65536, idle_timeout=60.0):
        data = FakeSSHRunner.remote_files.get(path)
        if data is None:
            raise RuntimeError("no such file: %s" % path)
        piece = data[start:start + length]
        for offset in range(0, len(piece), chunk):
            yield piece[offset:offset + chunk]

    def zip_files(self, paths, base, max_bytes, max_files):
        FakeSSHRunner.zipped = (tuple(paths), base, max_bytes, max_files)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("remote.txt", b"remote bytes")
        return buffer.getvalue()

    def send(self, pane_id, text, submit=True, pane=None):
        self.sent.append((self.host["id"], pane_id, text, submit))
        if FakeSSHRunner.auto_reply:
            match = re.search(r"TMUXWEB_REPLY_([0-9a-f]{24}):", text)
            if match:
                FakeSSHRunner.screen = "❯ " + match.group(0) + ' "' + FakeSSHRunner.auto_reply + '"\n'


class EnterUntilSubmittedTests(unittest.TestCase):
    """「文字进了 prompt 但没有回车」：屏幕不动就再按一次。"""

    class Backend(tmux_web.TmuxBackend):
        def __init__(self, screens):
            self.screens = list(screens)
            self.sent = []
        def _run(self, *args, **kwargs):
            self.sent.append(args)
            return ""
        def still_in_prompt(self, pane_id, text, lines=6):
            # "same" = 那行字还留在输入框里，需要再按一次
            return self.screens.pop(0) == "same" if self.screens else False

    def presses(self, backend):
        return [call for call in backend.sent if call[:2] == ("send-keys", "-t") and call[-1] == "C-m"]

    def test_one_enter_when_the_screen_moves(self):
        backend = self.Backend(["moved"])
        with patch("tmux_web.time.sleep"):
            backend.press_enter_until_submitted("%1", "hello")
        self.assertEqual(len(self.presses(backend)), 1)

    def test_presses_again_while_the_screen_is_frozen(self):
        backend = self.Backend(["same", "moved"])
        with patch("tmux_web.time.sleep"):
            backend.press_enter_until_submitted("%1", "hello")
        self.assertEqual(len(self.presses(backend)), 2)

    def test_gives_up_after_the_third_press(self):
        backend = self.Backend(["same"] * 20)
        with patch("tmux_web.time.sleep"):
            backend.press_enter_until_submitted("%1", "hello")
        self.assertEqual(len(self.presses(backend)), 3)

    def test_long_text_waits_longer(self):
        backend = self.Backend(["moved"])
        waits = []
        with patch("tmux_web.time.sleep", side_effect=lambda seconds: waits.append(seconds)):
            backend.press_enter_until_submitted("%1", "x" * 5000)
        self.assertGreater(waits[0], 1.0, "长文本先多等一下，别急着按回车")


class TmuxWebTests(unittest.TestCase):
    def setUp(self):
        self.tmux = FakeTmux()
        self.terminals = FakeTerminalFactory()
        self.remote_store = FakeRemoteStore()
        self.client = TestClient(create_app(
            self.tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=self.terminals,
            share_db=":memory:", queue_db=":memory:", needs_you_inbox_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=FakeSSHRunner,
        ))
        # 地址缓存是模块级的：上一个用例（或 startup 预热）算出来的东西会漏到下一个用例，
        # 让"我替换了 ip/tailscale 的几个用例"读到的其实是真机地址。每个用例从冷开始。
        import tmux_web
        tmux_web.ADDRESS_CACHE.update({"at": 0.0, "value": []})

    def login(self):
        response = self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def console_surface(self, path="/"):
        """The console page plus the two versioned assets it loads.

        The app module and the style bundle used to be inline in
        index.html; a marker that used to be in the page now lives in
        one of them, so tests that used to read the page read all three.
        """
        page = self.client.get(path).text
        app = self.client.get("/assets/console-app.js").text
        css = self.client.get("/assets/console.css").text
        return page + "\n" + app + "\n" + css

    def enable_prompt_card(self):
        """交互菜单卡片是实验性功能，默认关；要测它的用例先把它打开。"""
        self.login()
        self.assertEqual(self.client.put("/api/prefs", json={"prompt_card": "on"}).status_code, 200)

    def _fake_ip_output(self):
        return (
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "2: eno1    inet 10.168.236.31/23 brd 10.168.237.255 scope global eno1\n"
            "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
            "4: br-9c1    inet 172.18.0.1/16 brd 172.18.255.255 scope global br-9c1\n"
            "5: veth12ab    inet 172.19.0.7/16 brd 172.19.255.255 scope global veth12ab\n"
            # 云主机里的 QEMU/VirtualBox user-mode NAT：外面连不上
            "6: enp0s6    inet 10.0.2.12/24 brd 10.0.2.255 scope global dynamic enp0s6\n"
        )

    def _patch_addresses(self, serve="No serve config", json_dns="robo.neko-chroma.ts.net."):
        """直连地址端点靠外部命令取地址，测试里把 subprocess.run 换掉。

        缓存要先清掉：startup 事件会真的跑一遍 ip/tailscale（见 warm_addresses），
        不清的话这些用例读到的是跑测试那台机器的真实地址，跟假输出对不上。
        """
        from types import SimpleNamespace
        import subprocess
        import tmux_web

        tmux_web.ADDRESS_CACHE.update({"at": 0.0, "value": []})

        calls = []

        def fake_run(cmd, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            calls.append(joined)
            if cmd[:2] == ["ip", "-4"]:
                return SimpleNamespace(stdout=self._fake_ip_output(), stderr="", returncode=0)
            if joined.startswith("tailscale ip"):
                return SimpleNamespace(stdout="100.123.123.3\n", stderr="", returncode=0)
            if joined.startswith("tailscale status"):
                return SimpleNamespace(stdout='{"Self": {"DNSName": "%s"}}' % json_dns, stderr="", returncode=0)
            if joined.startswith("tailscale serve"):
                return SimpleNamespace(stdout=serve, stderr="", returncode=0)
            raise AssertionError("unexpected command: " + joined)

        return mock.patch.object(subprocess, "run", fake_run), calls

    def test_direct_addresses_skip_docker_bridges(self):
        """docker0/br-/veth 的地址用户连不上，列出来只会误导。"""
        self.login()
        patcher, _ = self._patch_addresses()
        with patcher:
            payload = self.client.get("/api/system/addresses").json()
        urls = [item["url"] for item in payload["addresses"]]
        self.assertIn("http://10.168.236.31:8787", urls)
        self.assertIn("http://100.123.123.3:8787", urls)
        self.assertFalse([u for u in urls if "172.1" in u or "127.0.0.1" in u], urls)

    def test_tailnet_https_entry_only_when_serve_is_configured(self):
        """没起 tailscale serve 的时候不能列 https 那条，点进去是连不上的。"""
        self.login()
        patcher, _ = self._patch_addresses()
        with patcher:
            # 地址列表现在是缓存的（云主机上 ip/tailscale 子进程能跑 6 秒）；换 mock 时要清掉
            tmux_web.ADDRESS_CACHE.update({"at": 0.0, "value": []})
            first = self.client.get("/api/system/addresses").json()
        self.assertFalse([a for a in first["addresses"] if a["url"].startswith("https://")],
                         first["addresses"])

        patcher, _ = self._patch_addresses(serve="https://robo.neko-chroma.ts.net (tailnet only)\n")
        with patcher:
            tmux_web.ADDRESS_CACHE.update({"at": 0.0, "value": []})
            second = self.client.get("/api/system/addresses").json()
        https = [a["url"] for a in second["addresses"] if a["url"].startswith("https://")]
        self.assertEqual(["https://robo.neko-chroma.ts.net"], https)

    def test_direct_addresses_need_a_login(self):
        response = self.client.get("/api/system/addresses", follow_redirects=False)
        self.assertIn(response.status_code, (303, 401, 403))

    def test_a_slow_capture_does_not_drop_the_live_socket(self):
        """慢链路（上传/下载大文件时）一次抓屏超时不该把聊天断开。"""
        self.login()
        calls = {"n": 0}
        original = self.tmux.capture_ansi

        def flaky(pane_id, history=500, pane=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("remote command timed out after 15s")
            return original(pane_id, history, pane=pane)

        self.tmux.capture_ansi = flaky
        with self.client.websocket_connect("/ws/panes/%254") as socket:
            payload = socket.receive_json()
        self.assertEqual(payload["type"], "capture")
        self.assertGreaterEqual(calls["n"], 3)

    def test_requires_login_and_rejects_wrong_password(self):
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/panes").status_code, 401)
        response = self.client.post("/login", data={"password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("unit-test-admin-password", response.text)

    def test_login_keeps_the_deep_link(self):
        """A shared ?pane= link must survive the login round trip."""
        redirect = self.client.get("/?pane=%2561", follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertIn("/login?next=", redirect.headers["location"])
        page = self.client.get(redirect.headers["location"])
        self.assertIn('name="next"', page.text)
        self.assertIn("/?pane=%2561", page.text)
        signed_in = self.client.post("/login", data={"password": "unit-test-admin-password", "next": "/?pane=%2561"},
                                     follow_redirects=False)
        self.assertEqual(signed_in.status_code, 303)
        self.assertEqual(signed_in.headers["location"], "/?pane=%2561")

    def test_login_refuses_to_bounce_off_site(self):
        for hostile in ("//evil.example", "https://evil.example", "/\\evil"):
            response = self.client.post("/login", data={"password": "unit-test-admin-password", "next": hostile},
                                        follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/", hostile)

    def test_admin_login_is_rate_limited(self):
        client = TestClient(create_app(self.tmux, password="rate-limit-password", session_secret="test-secret",
                                       terminal_factory=self.terminals, share_db=":memory:",
                                       remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner))
        for _ in range(5):
            self.assertEqual(client.post("/login", data={"password": "wrong"}).status_code, 401)
        limited = client.post("/login", data={"password": "rate-limit-password"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["detail"], "too many login attempts")

    def test_https_login_cookie_is_secure(self):
        client = TestClient(create_app(self.tmux, password="https-password", session_secret="test-secret",
                                       terminal_factory=self.terminals, share_db=":memory:",
                                       remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner),
                            base_url="https://testserver")
        response = client.post("/login", data={"password": "https-password"}, follow_redirects=False)
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_the_mobile_terminal_has_its_own_input_row(self):
        """The soft keyboard needs a real field: xterm's textarea must not summon it."""
        self.login()
        page = self.console_surface()
        for marker in ('id="terminal-input"', 'enterkeyhint="send"', "setupTerminalInput",
                       "setAttribute('inputmode','none')", "compositionstart"):
            self.assertIn(marker, page)
        # sizing must not repaint the screen or notify the PTY when nothing changed
        self.assertIn("function fitTerminalNow", page)
        self.assertNotIn("state.xterm.refresh(0,", page)
        # the keyboard field is invisible now (the terminal itself shows the caret), and it must
        # stay >=16px or iOS zooms the page in and never zooms back out
        self.assertIn(".terminal-kb-input{position:fixed", page)
        self.assertIn("font-size:16px;opacity:0", page)
        # the terminal view keeps its header row while composing, so the terminal shrinks
        # with the keyboard instead of growing by the 58px the header would give back
        self.assertIn(".app.terminal-view.mobile-composing .topbar{visibility:visible", page)
        self.assertIn(".app.terminal-view.mobile-composing .main{grid-template-rows:58px", page)
        # dismissing the keyboard must refit even when visualViewport stays quiet
        self.assertIn("function settleViewport", page)
        self.assertIn("settleViewport();", page)

    def test_the_terminal_input_is_a_keyboard_proxy(self):
        """A mirrored buffer cannot express "cursor left, then type" and re-sent whole lines."""
        self.login()
        page = self.console_surface()
        for marker in ("function withMods", "field.value=''", "flushTerminalPending",
                       "state.termPending", "Delete:'\\x1b[3~'"):
            self.assertIn(marker, page)
        # the old mirror compared the whole value and re-sent it when the cursor moved
        self.assertNotIn("value.startsWith(sent)", page)

    def test_lists_panes(self):
        self.login()
        response = self.client.get("/api/panes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["panes"][0]["id"], "%4")
        self.assertEqual(response.json()["panes"][0]["command"], "codex")

    def test_lists_host_tmux_tree_data(self):
        self.login()
        response = self.client.get("/api/hosts")
        self.assertEqual(response.status_code, 200)
        host = response.json()["hosts"][0]
        self.assertEqual(host["id"], "localhost")
        self.assertEqual(host["kind"], "local")
        self.assertEqual(host["status"], "online")
        self.assertEqual(host["panes"][0]["session"], "codex")
        self.assertEqual(host["panes"][0]["window_name"], "editor")

    def test_edits_a_remote_host_without_touching_the_secret(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        created = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        host_id = created["id"]

        renamed = self.client.put(f"/api/remote-hosts/{host_id}", json={"name": "builder", "username": "deploy"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["name"], "builder")
        self.assertEqual(renamed.json()["username"], "deploy")
        stored, credential = self.remote_store.hosts[host_id]
        self.assertEqual(credential, "secret")  # untouched when credential is omitted

        rotated = self.client.put(f"/api/remote-hosts/{host_id}", json={"credential": "new-secret"})
        self.assertEqual(rotated.status_code, 200)
        self.assertEqual(self.remote_store.hosts[host_id][1], "new-secret")

        switched = self.client.put(f"/api/remote-hosts/{host_id}", json={"auth_type": "agent"})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(self.remote_store.hosts[host_id][1], "")

        missing = self.client.put("/api/remote-hosts/nope", json={"name": "x"})
        self.assertEqual(missing.status_code, 404)

    def test_update_treats_empty_host_key_and_fingerprint_as_omitted(self):
        # A host saved before its first successful connection (like macmini)
        # stores an empty host_key; the switch and edit-save payloads carry
        # that empty string back, and the update must not 422 on it.
        empty = tmux_web.RemoteHostUpdateRequest(
            transport="agent", host_key="", fingerprint="")
        self.assertIsNone(empty.host_key)
        self.assertIsNone(empty.fingerprint)
        # a real key still passes through, and a too-short one still fails
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        keyed = tmux_web.RemoteHostUpdateRequest(
            host_key=host_key, fingerprint=host_key_fingerprint(host_key))
        self.assertEqual(keyed.host_key, host_key)
        with self.assertRaises(ValidationError):
            tmux_web.RemoteHostUpdateRequest(host_key="too-short", fingerprint="SHA256:xxxx")

    def test_transport_switch_works_on_a_host_without_a_host_key(self):
        self.login()
        created = self.client.post("/api/remote-hosts", json={
            "name": "macmini", "hostname": "10.0.0.7", "port": 22, "username": "mini",
            "auth_type": "password", "credential": "secret", "verify": False,
        }).json()
        host_id = created["id"]
        self.assertEqual(self.remote_store.hosts[host_id][0]["host_key"], "")

        # the minimal switch payload the UI now sends
        switched = self.client.put(f"/api/remote-hosts/{host_id}",
                                   json={"transport": "agent", "verify": False})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["transport"], "agent")
        self.assertEqual(self.remote_store.hosts[host_id][0]["host_key"], "")

        # the old full-payload shape (empty host_key/fingerprint included)
        # must not 422 either: the edit-save path sends it the same way when
        # the key scan fails and the save is skipped-verified
        edited = self.client.put(f"/api/remote-hosts/{host_id}", json={
            "name": "macmini", "hostname": "10.0.0.7", "port": 22, "username": "mini",
            "auth_type": "password", "transport": "ssh", "host_key": "", "fingerprint": "",
            "verify": False})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["transport"], "ssh")

    def test_empty_host_key_never_clears_a_pinned_key(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        created = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        host_id = created["id"]
        updated = self.client.put(f"/api/remote-hosts/{host_id}", json={
            "transport": "agent", "host_key": "", "fingerprint": "", "verify": False})
        self.assertEqual(updated.status_code, 200)
        stored = self.remote_store.hosts[host_id][0]
        self.assertEqual(stored["host_key"], host_key)
        self.assertEqual(stored["fingerprint"], fingerprint)
        self.assertEqual(stored["transport"], "agent")

    def test_rejects_an_edit_that_would_break_the_host(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        created = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        host_id = created["id"]

        # a mismatched fingerprint is refused and nothing changes
        bad = self.client.put(f"/api/remote-hosts/{host_id}", json={
            "host_key": host_key, "fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(self.remote_store.hosts[host_id][0]["hostname"], "10.0.0.8")

        # password auth without a credential is impossible
        self.client.put(f"/api/remote-hosts/{host_id}", json={"auth_type": "agent"})
        blocked = self.client.put(f"/api/remote-hosts/{host_id}", json={"auth_type": "password"})
        self.assertEqual(blocked.status_code, 422)

        # an unreachable host rolls the edit back
        original = dict(self.remote_store.hosts[host_id][0])
        with patch.object(FakeSSHRunner, "check", side_effect=RuntimeError("Permission denied")):
            failing = self.client.put(f"/api/remote-hosts/{host_id}", json={"username": "nobody"})
        self.assertEqual(failing.status_code, 424)
        self.assertIn("Permission denied", failing.json()["detail"])
        self.assertEqual(self.remote_store.hosts[host_id][0]["username"], original["username"])

    def test_a_host_may_be_saved_without_a_working_credential(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        payload = {
            "name": "half-configured", "hostname": "10.0.0.9", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "not-the-password", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }
        with patch.object(FakeSSHRunner, "check", side_effect=RuntimeError("Permission denied")):
            refused = self.client.post("/api/remote-hosts", json=payload)
            saved = self.client.post("/api/remote-hosts", json={**payload, "verify": False})
        self.assertEqual(refused.status_code, 424)
        self.assertEqual(saved.status_code, 201)
        host_id = saved.json()["id"]
        self.assertIn(host_id, self.remote_store.hosts)

        # editing with verify=false keeps the change even though auth is broken
        with patch.object(FakeSSHRunner, "check", side_effect=RuntimeError("Permission denied")):
            edited = self.client.put(f"/api/remote-hosts/{host_id}",
                                     json={"username": "someone", "verify": False})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(self.remote_store.hosts[host_id][0]["username"], "someone")

    def test_interrupt_uses_the_key_each_cli_expects(self):
        self.login()
        # hermes interrupts on Ctrl+Q (Ctrl+C also interrupts but a second press
        # force-exits the TUI), the other CLIs on Escape
        self.assertEqual(tmux_web.interrupt_key("hermes"), "C-q")
        for provider in ("claude", "codex", "kimi", None):
            self.assertEqual(tmux_web.interrupt_key(provider), "Escape")

        sent = []
        self.tmux.panes[0]["command"] = "python3"  # provider_for_command -> hermes
        self.tmux.send_key = lambda pane_id, key, pane=None: sent.append((pane_id, key))
        response = self.client.post("/api/panes/%254/interrupt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["key"], "C-q")
        self.assertEqual(sent, [("%4", "C-q")])

    def test_escape_can_be_sent_to_interrupt_a_turn(self):
        self.login()
        sent = []
        self.tmux.send_key = lambda pane_id, key, pane=None: sent.append((pane_id, key))
        response = self.client.post("/api/panes/%254/key", json={"key": "Escape"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent, [("%4", "Escape")])

        # only the named keys the UI needs are accepted
        self.assertEqual(self.client.post("/api/panes/%254/key", json={"key": "rm -rf /"}).status_code, 422)
        self.client.cookies.clear()
        self.assertIn(self.client.post("/api/panes/%254/key", json={"key": "Escape"}).status_code, (401, 403))

    def test_key_endpoint_accepts_the_single_character_keys_a_menu_answer_needs(self):
        """菜单答案就是一个字符 + Enter（y/n、编号菜单的数字）：/key 必须能发它们，
        否则等你卡片只能答光标菜单。单个字符以外还是 422。"""
        self.login()
        sent = []
        self.tmux.send_key = lambda pane_id, key, pane=None: sent.append((pane_id, key))
        for key in ("y", "n", "2"):
            response = self.client.post("/api/panes/%254/key", json={"key": key})
            self.assertEqual(response.status_code, 200, key)
        self.assertEqual(sent, [("%4", "y"), ("%4", "n"), ("%4", "2")])
        for key in ("ab", "yes", ";", "C-x"):
            self.assertEqual(self.client.post("/api/panes/%254/key", json={"key": key}).status_code,
                             422, key)

    def test_send_button_turns_into_a_stop_button_while_working(self):
        self.login()
        page = self.console_surface()
        for marker in ('id="i-stop"', "function stopAgent(", "function syncSendButton(",
                       "/interrupt`,{method:'POST'}", ".send.stop{background:#b34b43}",
                       "停止当前推理（Esc）"):
            self.assertIn(marker, page)

    def test_a_host_can_be_saved_without_a_fingerprint(self):
        self.login()
        base = {"name": "no-scan", "hostname": "10.255.255.1", "port": 22, "username": "nobody",
                "auth_type": "password", "credential": "x"}
        strict = self.client.post("/api/remote-hosts", json=dict(base, verify=False, host_key=""))
        # verification may be skipped, but a verified host still needs a pinned key
        self.assertEqual(strict.status_code, 201)
        self.assertEqual(strict.json()["fingerprint"], "")
        verified = self.client.post("/api/remote-hosts",
                                    json=dict(base, name="no-scan-2", verify=True, host_key=""))
        self.assertEqual(verified.status_code, 422)

    def test_host_form_lets_you_reveal_and_skip_verification(self):
        self.login()
        page = self.client.get("/hosts").text
        for marker in ('id="show-secret"', 'id="skip-verify"', 'id="edit-show-secret"',
                       'id="edit-skip-verify"', "function applySecret(", ".field.host-row{",
                       ".field{display:grid;gap:5px;grid-template-columns:minmax(0,1fr)",
                       "verify:!skipVerify", "payload.verify=!skipVerify"):
            self.assertIn(marker, page)

    def test_reorders_remote_hosts(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        ids = []
        for name in ("alpha", "beta", "gamma"):
            created = self.client.post("/api/remote-hosts", json={
                "name": name, "hostname": f"10.0.0.{len(ids) + 8}", "port": 22, "username": "ubuntu",
                "auth_type": "agent", "credential": "", "host_key": host_key,
                "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
            }).json()
            ids.append(created["id"])
        self.assertEqual(list(self.remote_store.hosts), ids)

        reordered = self.client.post("/api/remote-hosts/reorder", json={"ids": ids[::-1]})
        self.assertEqual(reordered.status_code, 200)
        self.assertEqual([host["id"] for host in reordered.json()["hosts"]], ids[::-1])
        self.assertEqual([host["id"] for host in self.client.get("/api/remote-hosts").json()["hosts"]], ids[::-1])

        partial = self.client.post("/api/remote-hosts/reorder", json={"ids": ids[:2]})
        self.assertEqual(partial.status_code, 422)

    def test_a_hanging_remote_host_does_not_break_the_tree(self):
        self.login()
        host_key = "10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        created = self.client.post("/api/remote-hosts", json={
            "name": "hung-box", "hostname": "10.0.0.9", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint
        })
        self.assertEqual(created.status_code, 201)
        host_id = created.json()["id"]

        class HangingRunner:
            def __init__(self, host):
                self.host = host

            def ping(self, timeout=3):
                # answers the handshake: the host is up, only the listing hangs
                return None

            def list_panes(self, timeout=15):
                raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=timeout)

        client = TestClient(create_app(
            self.tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=self.terminals,
            share_db=":memory:", queue_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=HangingRunner,
        ))
        logged_in = client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(logged_in.status_code, 303)

        response = client.get("/api/hosts")
        self.assertEqual(response.status_code, 200)
        hosts = response.json()["hosts"]
        local = next(host for host in hosts if host["id"] == "localhost")
        self.assertEqual(local["status"], "online")
        self.assertNotEqual(local["panes"], [])
        hung = next(host for host in hosts if host["id"] == host_id)
        # The ping answered, so the host is online; only its pane listing timed
        # out, and that is reported as an error on the row, not as "offline".
        self.assertEqual(hung["status"], "online")
        self.assertIn("timed out", hung["error"])
        self.assertEqual(hung["panes"], [])

    def test_a_failed_listing_keeps_the_last_panes_and_marks_them_stale(self):
        self.login()
        host_key = "10.0.0.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        created = self.client.post("/api/remote-hosts", json={
            "name": "slow-box", "hostname": "10.0.0.10", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint
        })
        self.assertEqual(created.status_code, 201)
        host_id = created.json()["id"]

        class FlakyRunner:
            """Lists once, then hangs: the link got slow, the host did not die."""
            calls = 0

            def __init__(self, host):
                self.host = host

            connected = True

            def ping(self, timeout=3):
                if not self.connected:
                    raise OSError("unreachable")

            def list_panes(self, timeout=15):
                FlakyRunner.calls += 1
                if FlakyRunner.calls == 2:
                    raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=timeout)
                return [{"id": f"{self.host['id']}~%21", "pane_id": "%21",
                         "host_id": self.host["id"], "target": "slow:0.0",
                         "session": "slow", "window": "0", "window_name": "shell",
                         "index": "0", "command": "zsh", "title": "kept",
                         "path": "/srv/slow", "active": True, "width": 80, "height": 24}]

        client = TestClient(create_app(
            self.tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=self.terminals,
            share_db=":memory:", queue_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=FlakyRunner,
        ))
        client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)

        first = next(host for host in client.get("/api/hosts").json()["hosts"] if host["id"] == host_id)
        self.assertEqual(first["status"], "online")
        self.assertEqual(len(first["panes"]), 1)
        self.assertNotIn("stale", first)

        # Second round: same host, listing hangs. The cached pane list has to
        # survive (flagged stale) instead of the row collapsing to 0 panes.
        again = next(host for host in
                     client.get(f"/api/hosts?refresh=1&host={host_id}").json()["hosts"]
                     if host["id"] == host_id)
        self.assertEqual(again["status"], "online")
        self.assertTrue(again["stale"])
        self.assertIn("timed out", again["error"])
        self.assertEqual([pane["pane_id"] for pane in again["panes"]], ["%21"])
        FlakyRunner.connected = False
        url = "/api/hosts?refresh=1&host=" + host_id
        down = next(h for h in client.get(url).json()["hosts"] if h["id"] == host_id)
        self.assertEqual((down["status"], down["listing_state"], down["panes"]),
                         ("offline", "cached", first["panes"]))
        FlakyRunner.connected = True
        restored = next(h for h in client.get(url).json()["hosts"] if h["id"] == host_id)
        self.assertEqual((restored["status"], restored["listing_state"]), ("online", "fresh"))
        self.assertNotIn("error", restored)
        self.assertFalse(restored.get("stale"))

    def test_adds_remote_host_and_routes_remote_pane_operations(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        created = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint
        })
        self.assertEqual(created.status_code, 201)
        host_id = created.json()["id"]
        hosts = self.client.get("/api/hosts").json()["hosts"]
        remote = next(host for host in hosts if host["id"] == host_id)
        self.assertEqual(remote["status"], "online")
        pane_id = remote["panes"][0]["id"]
        capture = self.client.get(f"/api/panes/{pane_id}/capture")
        self.assertEqual(capture.status_code, 200)
        self.assertIn("REMOTE_OK", capture.json()["content"])
        sent = self.client.post(f"/api/panes/{pane_id}/input", json={"text": "hello", "submit": True})
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(FakeSSHRunner.sent[-1], (host_id, "%9", "hello", True))
        transcript = self.client.get(f"/api/panes/{pane_id}/transcript")
        self.assertEqual(transcript.status_code, 200)
        self.assertEqual(transcript.json()["session_id"], "remote-session-1")

    def test_remote_connection_can_be_tested_without_saving(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        response = self.client.post("/api/remote-hosts/test", json={
            "name": "test-only", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ssh_ok"])
        self.assertEqual(response.json()["pane_count"], 1)
        self.assertEqual(self.remote_store.list_hosts(), [])

    def test_remote_host_test_stays_inside_a_total_budget(self):
        """check + list_panes 曾经各按自己的超时串行等：总预算 25s（默认）内必须
        做完；预算改小后两步的超时都跟着收紧，而不是各等各的。"""
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        payload = {
            "name": "test-only", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }
        response = self.client.post("/api/remote-hosts/test", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FakeSSHRunner.last_check_timeout, 12)
        self.assertLessEqual(FakeSSHRunner.last_list_timeout, 25)
        with patch.dict("os.environ", {"TMUX_WEB_HOST_TEST_BUDGET": "10"}):
            response = self.client.post("/api/remote-hosts/test", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FakeSSHRunner.last_check_timeout, 10)
        self.assertLessEqual(FakeSSHRunner.last_list_timeout, 10)

    def test_captures_selected_pane(self):
        self.login()
        response = self.client.get("/api/panes/%254/capture")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], self.tmux.output)

    def test_sends_literal_text_and_optional_enter(self):
        self.login()
        response = self.client.post(
            "/api/panes/%254/input", json={"text": "hello; rm -rf /", "submit": False}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tmux.sent, [("%4", "hello; rm -rf /", False)])

    def test_real_tmux_send_waits_for_paste_before_submit(self):
        backend = TmuxBackend()
        calls = []
        backend.validate_pane = lambda pane_id: {"id": pane_id, "command": "claude"}
        backend._run = lambda *args, input_bytes=None: calls.append((args, input_bytes)) or ""
        with patch("tmux_web.time.sleep") as sleep:
            backend.send("%4", "hello", True)
        self.assertEqual(calls[0][0], ("send-keys", "-t", "%4", "C-u"))
        self.assertEqual(calls[1][0][0], "load-buffer")
        self.assertEqual(calls[2][0][0], "paste-buffer")
        # 粘贴后先等一拍再回车；屏幕没动会再补一次（这里 screen() 是空的，等于一直没动）
        self.assertEqual(sleep.call_args_list[0].args[0], 0.05)
        self.assertGreaterEqual(sleep.call_args_list[1].args[0], 0.12)
        enters = [call[0] for call in calls if call[0][:2] == ("send-keys", "-t") and call[0][-1] == "C-m"]
        self.assertGreaterEqual(len(enters), 1)
        self.assertEqual(enters[0], ("send-keys", "-t", "%4", "C-m"))

    def test_reverse_jsonl_reader_returns_tail_without_full_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_bytes(b"x" * (2 * 1024 * 1024) + b"\nfirst\nsecond\nthird\n")
            lines = list(TmuxBackend._reverse_lines(path, chunk_size=1024))
        self.assertEqual(lines[:3], ["third", "second", "first"])

    def test_reads_claude_transcript(self):
        self.tmux.panes[0]["command"] = "claude"
        self.login()
        response = self.client.get("/api/panes/%254/transcript")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][1]["role"], "assistant")

    def test_unchanged_transcript_poll_skips_the_reread(self):
        """A poll whose sig matches and whose session file is untouched must not re-read."""
        self.login()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write("{}\n")
            session_path = handle.name
        self.addCleanup(os.unlink, session_path)
        calls = []

        def fake_transcript(pane_id, limit=200, refresh=False, pane=None):
            calls.append(pane_id)
            return {"pane": pane_id, "session_id": "session-1",
                    "mtime": os.path.getmtime(session_path), "has_more": False,
                    "messages": [{"id": "u1", "role": "user", "text": "问题", "timestamp": None},
                                 {"id": "a1", "role": "assistant", "text": "答", "timestamp": None}],
                    "path": session_path}

        with patch.object(self.tmux, "transcript", side_effect=fake_transcript):
            first = self.client.get("/api/panes/%254/transcript")
            self.assertEqual(first.status_code, 200)
            sig = first.json()["signature"]
            self.assertEqual(len(calls), 1)
            second = self.client.get("/api/panes/%254/transcript", params={"sig": sig})
            self.assertEqual(second.json(), {"unchanged": True, "signature": sig, "waiting": False,
                                            "waiting_since": None, "has_more": False})
            self.assertEqual(len(calls), 1)
            Path(session_path).write_text("{}\n{}\n")
            third = self.client.get("/api/panes/%254/transcript", params={"sig": sig})
            self.assertEqual(len(calls), 2)
            self.assertNotIn("unchanged", third.json())

    def test_reads_kimi_transcript_without_thinking_tools_or_notifications(self):
        self.tmux.panes[0].update({"command": "kimi", "title": "Kimi test", "path": "/work/kimi"})
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            session = home / ".kimi-code" / "sessions" / "wd_test" / "session_test"
            wire = session / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            (session / "state.json").write_text(json.dumps({
                "id": "session_test", "cwd": "/work/kimi", "title": "Kimi test",
                "archived": False, "updatedAt": 123,
            }))
            events = [
                {"type": "agent.message.appended", "time": 1000, "message": {"message": {
                    "role": "user", "content": [{"type": "text", "text": "用户问题"}]},
                    "meta": {"source": "input", "userMessageId": "user-1"}}},
                {"type": "agent.message.appended", "time": 1100, "message": {"message": {
                    "role": "assistant", "content": [{"type": "think", "think": "hidden reasoning"},
                    {"type": "text", "text": "可见回答"}], "toolCalls": [{"name": "Read"}]},
                    "meta": {"source": "llm", "messageId": "assistant-1"}}},
                {"type": "agent.message.appended", "time": 1200, "message": {"message": {
                    "role": "tool", "content": [{"type": "text", "text": "secret tool output"}]},
                    "meta": {"source": "tool"}}},
                {"type": "agent.message.appended", "time": 1300, "message": {"message": {
                    "role": "user", "content": [{"type": "text", "text": "internal notification"}]},
                    "meta": {"source": "notify"}}},
            ]
            wire.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
            backend = TmuxBackend()
            backend.validate_pane = lambda pane_id: self.tmux.panes[0]
            with patch("tmux_web.Path.home", return_value=home):
                result = backend.transcript("%4", 20)
        self.assertEqual(result["provider"], "kimi")
        self.assertEqual(result["session_id"], "session_test")
        self.assertEqual([(item["role"], item["text"]) for item in result["messages"]], [
            ("user", "用户问题"), ("assistant", "可见回答")
        ])

    def test_reads_kimi_2x_wire_where_the_answer_is_streamed_parts(self):
        """kimi 2.3.3：用户是 context.append_message，回答是 content.part 的流。"""
        self.tmux.panes[0].update({"command": "kimi", "title": "配置 provider", "path": "/Users/me"})
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            session = home / ".kimi-code" / "sessions" / "wd_me_abc" / "session_2ff99be8"
            wire = session / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            (session / "state.json").write_text(json.dumps({
                "id": "session_2ff99be8", "cwd": "/Users/me", "title": "配置 provider",
                "archived": False, "updatedAt": 1794011808758,
            }))
            events = [
                {"type": "turn.prompt", "time": 1788788631968,
                 "input": [{"type": "text", "text": "配置 hermes provider"}],
                 "promptId": "msg_1"},
                {"type": "context.append_message", "time": 1788788631,
                 "message": {"role": "user", "origin": {"kind": "user"}, "id": "msg_1",
                             "content": [{"type": "text", "text": "配置 hermes provider"}]}},
                {"type": "context.append_loop_event", "time": 1788788631976,
                 "event": {"type": "step.begin", "turnId": "0", "step": 1, "uuid": "step-a"}},
                {"type": "context.append_loop_event", "time": 1788788632000,
                 "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "step-a",
                           "part": {"type": "think", "think": "私下的推理"}}},
                {"type": "context.append_loop_event", "time": 1788788632100,
                 "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "step-a",
                           "part": {"type": "text", "text": "我先看一下本地配置。"}}},
                {"type": "context.append_loop_event", "time": 1788788632200,
                 "event": {"type": "content.part", "turnId": "0", "step": 1, "stepUuid": "step-a",
                           "part": {"type": "text", "text": "然后改 provider。"}}},
                {"type": "tool.call", "time": 1788788632300, "uuid": "tool-1"},
                {"type": "context.append_message", "time": 1788788700,
                 "message": {"role": "user", "origin": {"kind": "user"}, "id": "msg_2",
                             "content": [{"type": "text", "text": "谢谢"}]}},
            ]
            wire.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
            backend = TmuxBackend()
            backend.validate_pane = lambda pane_id: self.tmux.panes[0]
            with patch("tmux_web.Path.home", return_value=home):
                result = backend.transcript("%4", 20)
        self.assertEqual(result["provider"], "kimi")
        messages = [(item["role"], item["text"]) for item in result["messages"]]
        self.assertEqual(messages, [
            ("user", "配置 hermes provider"),
            ("assistant", "我先看一下本地配置。\n\n然后改 provider。"),
            ("user", "谢谢"),
        ], "同一 step 的 text 片段要拼成一条，think 不能露出来")

    def test_a_transcript_still_reads_when_the_pane_pid_is_unavailable(self):
        """拿不到 pane_pid（没有 tmux server / 调用方自带 pane dict）只是少了 --resume 提示，不能整条 transcript 报错。"""
        backend = TmuxBackend()
        pane = {"id": "%4", "command": "claude", "title": "t", "path": "/work/x"}
        backend.validate_pane = lambda pane_id: pane

        def boom(pane_id):
            raise TmuxError("error connecting to /tmp/tmux-0/default")

        backend.pane_pid = boom
        with patch("transcript_reader.read_transcript", return_value={"messages": []}) as reader:
            data = backend.transcript("%4", 5)
        self.assertEqual(data["messages"], [])
        self.assertIsNone(reader.call_args.kwargs["hint"])
        self.assertEqual(reader.call_args.args[:2], ("claude", "/work/x"))

    def test_kimi_picks_the_session_that_is_still_being_written(self):
        """Two sessions can share a cwd and a title; the newest one holds the messages."""
        self.tmux.panes[0].update({"command": "kimi", "title": "继续完成工作", "path": "/work/kimi"})
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".kimi-code" / "sessions" / "wd_test"
            def make(name, updated_at, events):
                session = root / f"session_{name}"
                wire = session / "agents" / "main" / "wire.jsonl"
                wire.parent.mkdir(parents=True)
                (session / "state.json").write_text(json.dumps({
                    "id": f"session_{name}", "cwd": "/work/kimi", "title": "继续完成工作",
                    "archived": False, "updatedAt": updated_at,
                }))
                wire.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
            # an old, aborted session: its tail only has bookkeeping records
            make("old", 1790000000000, [{"type": "file_history.checkpoint", "turnId": 3},
                                        {"type": "prompt.completed", "reason": "failed"}])
            make("live", 1793173903861, [{"type": "agent.message.appended", "time": 2000, "message": {
                "message": {"role": "assistant", "content": [{"type": "text", "text": "最新回答"}]},
                "meta": {"source": "llm", "messageId": "assistant-live"}}}])

            backend = TmuxBackend()
            backend.validate_pane = lambda pane_id: self.tmux.panes[0]
            with patch("tmux_web.Path.home", return_value=home):
                result = backend.transcript("%4", 20)
        self.assertEqual(result["session_id"], "session_live")
        self.assertEqual([item["text"] for item in result["messages"]], ["最新回答"])

    def test_pausing_keeps_the_terminal_on_screen(self):
        """Pausing stops the stream; it must not dispose the xterm and leave a black screen."""
        self.login()
        page = self.console_surface("/?pane=%254")
        # closeConnections() takes a flag: pause keeps the canvas, everything else tears it down
        self.assertIn("function closeConnections(keepTerminal){", page)
        self.assertIn("if(!keepTerminal&&state.xterm){state.xterm.dispose();", page)
        self.assertIn("state.livePaused=true;closeConnections(true);", page)
        # resuming re-attaches into the canvas that is already on screen, so there is no black gap
        self.assertIn("function connectTerminal(id,reuse){", page)
        self.assertIn("if(state.mode==='terminal')connectTerminal(state.selected,true);", page)
        # the frozen screen is only cleared once the new attach actually sends something
        self.assertIn("let fresh=reused;", page)
        self.assertIn("if(fresh){fresh=false;", page)
        self.assertIn("try{xterm.reset();}catch{}", page)
        # one input binding per terminal: rebinding on every (re)connect doubled every keystroke
        self.assertIn("xterm.onData(data=>{const live=state.termWs;", page)
        self.assertNotIn("state.termDataSub", page)
        # the paused label survives the ws close event and the progress ticker
        self.assertIn("if(state.livePaused){$('status-dot').className='status-dot paused';$('status-text').textContent='已暂停实时刷新（点 ▶ 继续）';return;}", page)
        self.assertIn(".status-dot.paused{background:var(--faint)}", page)

    def test_the_send_status_line_reports_failure_and_clears_after_a_retry(self):
        """The composer's status line is neutral by default, red on failure, and a retry must reset it."""
        self.login()
        page = self.console_surface("/?pane=%254")
        self.assertIn(".send-state{min-height:17px;color:var(--muted)}", page)
        self.assertIn(".send-state.ok{color:var(--green)}", page)
        self.assertIn(".send-state.bad{color:var(--danger)}", page)
        self.assertIn("function setSendState(text,kind){", page)
        self.assertIn("setSendState('发送失败','bad');", page)
        # the bug: a failed send left 发送失败 on screen (in green) even after the retry worked
        resend = page[page.index("async function resendPending(localId){"):]
        resend = resend[:resend.index("async function sendInput(")]
        self.assertIn("setSendState('重发中…')", resend)
        self.assertIn("setSendState('已发送 ✓','ok')", resend)
        # nothing writes the status line directly any more, so stale text cannot survive
        self.assertNotIn("$('send-state').textContent", page)

    def test_message_rows_show_the_time_next_to_the_copy_button(self):
        """Each bubble's action row carries the message timestamp on the left."""
        self.login()
        page = self.console_surface("/?pane=%254")
        self.assertIn("clockTime=ts=>", page)
        self.assertIn('<span class="msg-time">', page)
        self.assertIn(".msg-actions .msg-time{", page)
        # transcripts hand out ISO strings as well as epoch numbers
        self.assertIn("new Date(ts)", page)
        # the row centres its items: a stretch alignment put the time above the button
        self.assertIn(".msg-actions{display:flex;align-items:center;", page)
        self.assertIn("d.toDateString()===now.toDateString()", page)

        preview = self.client.get("/preview?pane=%254").text
        self.assertIn("const clockTime=ts=>", preview)
        self.assertIn('<span class="msg-time">', preview)
        self.assertIn(".msg-tools .msg-time{", preview)
        self.assertIn(".msg-tools{display:flex;align-items:center;", preview)

    def test_a_touch_device_does_not_autofocus_the_composer(self):
        """手机上点进聊天就弹键盘很烦；桌面上仍然自动聚焦。判据是粗指针/无悬停，不是宽度。"""
        self.login()
        page = self.console_surface()
        self.assertIn("function isTouch(){return matchMedia('(pointer:coarse)').matches", page)
        self.assertIn("function canAutoFocusComposer(){return !isMobile()&&!isTouch();}", page)
        self.assertIn("if(state.mode==='chat'&&!relayView&&canAutoFocusComposer())input.focus();", page)
        self.assertIn("if(mode==='chat'&&state.selected&&canAutoFocusComposer())input.focus();", page)
        self.assertNotIn("if(state.mode==='chat'&&!isMobile())input.focus();", page)

    def test_fullscreen_follows_the_visual_viewport_on_a_phone(self):
        """手机上全屏写死 100vh，键盘一弹出输入框就被顶到键盘底下（只露出上半截）。"""
        self.login()
        page = self.console_surface()
        self.assertIn(".main:fullscreen,.main.fullscreen-fallback{width:100vw;height:var(--visual-height,100dvh);", page)
        self.assertIn("max-height:var(--visual-height,100dvh)}", page)
        self.assertIn("padding-bottom:calc(9px + env(safe-area-inset-bottom,0px) + 4px)}", page)

    def test_labelled_buttons_cannot_be_squeezed_into_overflow(self):
        """带字的按钮一律 flex:0 0 auto + nowrap：窄屏时被挤扁，文字就会顶出边框（对话全屏 那次）。"""
        self.login()
        page = self.console_surface()
        self.assertIn(".share-btn,.fullscreen-btn{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;white-space:nowrap}", page)
        # 手机上仍然是图标按钮（文字藏起来），不会挤
        self.assertIn(".fullscreen-btn{width:34px;padding:0;justify-content:center}", page)
        self.assertIn(".fullscreen-btn span{display:none}", page)

    def test_a_block_copy_button_shows_its_label(self):
        """块顶栏的「复制 / 源码」按钮：图标没有宽高时 svg 是 300×150，会把文字顶出按钮。"""
        self.login()
        page = self.console_surface()
        self.assertIn(".msg-copy .ico,.block-btn .ico{width:11px;height:11px;", page)
        self.assertIn(".msg-copy,.block-btn{display:inline-flex;align-items:center}", page)
        # 文字单独一层：复制成功只换字，不会把图标删掉
        self.assertEqual(page.count('<span class="btn-label">复制全部</span>'), 1)
        self.assertIn('<span class="btn-label">复制</span>', page)
        self.assertIn("const label=button.querySelector('.btn-label')||button;", page)
        self.assertIn("label.textContent=ok?'已复制':'复制失败';", page)

    def test_a_wide_table_cannot_push_the_copy_row_off_screen(self):
        """手机上宽表格：表格在自己的框里横滚，气泡宽度被夹住，复制行留在气泡里且不会被挤出去。"""
        self.login()
        page = self.console_surface()
        # The shared bubble template keeps the action row inside the message body.
        self.assertIn('<div class="assistant-body">${text??markdown(message.text)}${copy}</div>', page)
        self.assertNotIn('class="assistant-wrap"', page)
        self.assertIn(".assistant-body>.msg-actions{margin-top:12px;padding-top:8px;border-top:1px solid var(--line)}", page)
        # 气泡自己限制宽度并裁掉溢出（fit-content 不会再被宽内容撑大）
        self.assertIn(".assistant-body{width:fit-content;max-width:min(92%,820px);min-width:0;overflow-x:clip;", page)
        self.assertIn(".block-wrap{position:relative;margin:10px 0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--surface2);max-width:100%;min-width:0}", page)
        self.assertIn(".table-holder{padding:8px 10px;overflow:auto;max-width:100%;", page)
        self.assertIn(".assistant-body .table-holder table{display:table;width:max-content;min-width:100%;max-width:none;", page)
        # 触屏没有悬停：复制行常显（0.45 的透明度在手机上像"没有了"）
        self.assertIn(".msg-actions{display:flex;align-items:center;gap:6px;margin-top:8px;justify-content:flex-end;opacity:.62;", page)
        self.assertIn("@media (hover:none){.msg-actions{opacity:1}}", page)


    def test_an_agent_pane_does_not_fall_back_to_its_screen(self):
        """Streaming a full-screen TUI is what used to lock up the browser."""
        self.login()
        page = self.console_surface()
        for marker in ("isAgentPane", "showTranscriptNotice", "retryTranscript",
                       "transcript-empty", "CAPTURE_MAX_BYTES"):
            self.assertIn(marker, page)

    def test_creates_password_protected_share_and_allows_chat(self):
        self.tmux.panes[0]["command"] = "claude"
        self.login()
        response = self.client.post("/api/shares", json={
            "pane_id": "%4", "slug": "share001", "password": "guest-pass", "expires_in": 3600
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["url"], "/share/share001")

        guest = TestClient(self.client.app)
        share_page = guest.get("/share/share001")
        self.assertEqual(share_page.status_code, 200)
        self.assertIn('/share-assets/marked.esm.js', share_page.text)
        self.assertIn('marked.parse', share_page.text)
        self.assertIn('.markdown table', share_page.text)
        self.assertEqual(guest.get('/share-assets/marked.esm.js').status_code, 200)
        self.assertEqual(guest.get('/assets/marked.esm.js').status_code, 401)
        self.assertEqual(guest.get("/share/share001/api/transcript").status_code, 401)
        self.assertEqual(guest.post("/share/share001/login", data={"password": "wrong"}).status_code, 401)
        login = guest.post("/share/share001/login", data={"password": "guest-pass"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        transcript = guest.get("/share/share001/api/transcript")
        self.assertEqual(transcript.status_code, 200)
        self.assertEqual(transcript.json()["session_id"], "session-1")
        sent = guest.post("/share/share001/api/input", json={"text": "继续分析", "submit": True})
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(self.tmux.sent[-1], ("%4", "继续分析", True))
        self.assertEqual(guest.get("/api/hosts").status_code, 401)

    def test_share_expiry_and_revocation_are_enforced(self):
        self.tmux.panes[0]["command"] = "claude"
        self.login()
        created = self.client.post("/api/shares", json={
            "pane_id": "%4", "slug": "temporary", "password": "", "expires_in": 3600
        }).json()
        guest = TestClient(self.client.app)
        self.assertEqual(guest.get("/share/temporary/api/transcript").status_code, 200)
        revoked = self.client.post(f"/api/shares/{created['id']}/stop")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(guest.get("/share/temporary/api/transcript").status_code, 410)

    def test_read_only_share_rejects_input_but_serves_history(self):
        self.tmux.panes[0]["command"] = "claude"
        self.login()
        created = self.client.post("/api/shares", json={
            "pane_id": "%4", "slug": "frozen", "password": "", "expires_in": 3600, "allow_input": False,
        }).json()
        self.assertFalse(created["allow_input"])

        guest = TestClient(self.client.app)
        page = guest.get("/share/frozen")
        self.assertEqual(page.status_code, 200)
        self.assertIn("allowInput=false", page.text)
        self.assertEqual(guest.get("/share/frozen/api/transcript").status_code, 200)

        sent = guest.post("/share/frozen/api/input", json={"text": "hi"})
        self.assertEqual(sent.status_code, 403)
        self.assertFalse(self.tmux.sent)

        meta = guest.get("/share/frozen/api/meta")
        self.assertEqual(meta.status_code, 200)
        self.assertEqual(meta.json()["slug"], "frozen")
        self.assertFalse(meta.json()["allow_input"])
        self.assertEqual(meta.json()["mode"], "transcript")
        self.assertFalse(meta.json()["password_protected"])

        listed = next(item for item in self.client.get("/api/shares").json()["shares"] if item["id"] == created["id"])
        self.assertFalse(listed["allow_input"])

        writable = self.client.post("/api/shares", json={
            "pane_id": "%4", "slug": "chatty", "password": "", "expires_in": 3600,
        }).json()
        self.assertTrue(writable["allow_input"])
        page = guest.get("/share/chatty")
        self.assertIn("allowInput=true", page.text)
        self.assertEqual(guest.post("/share/chatty/api/input", json={"text": "hi"}).status_code, 200)
        self.assertTrue(self.tmux.sent)

    def test_share_table_gains_allow_input_column_when_upgrading(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE shares (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, pane_id TEXT NOT NULL,"
            " provider TEXT NOT NULL, session_id TEXT NOT NULL, title TEXT NOT NULL, password_hash TEXT,"
            " created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL)"
        )
        connection.execute("INSERT INTO shares VALUES ('s1','legacy','%4','claude','x','t',NULL,1,2,NULL)")
        ensure_share_schema(connection)
        self.assertIn("allow_input", {row[1] for row in connection.execute("PRAGMA table_info(shares)")})
        self.assertEqual(connection.execute("SELECT allow_input FROM shares WHERE id='s1'").fetchone()[0], 1)
        ensure_share_schema(connection)  # idempotent
        connection.close()

    def test_admin_can_extend_reset_password_stop_and_delete_share(self):
        self.tmux.panes[0]["command"] = "claude"
        self.login()
        created = self.client.post("/api/shares", json={
            "pane_id": "%4", "slug": "managed-share", "password": "old-pass", "expires_in": 3600
        }).json()
        share_id = created["id"]
        before = created["expires_at"]

        page = self.client.get("/shares")
        self.assertEqual(page.status_code, 200)
        self.assertIn("分享管理", page.text)
        self.assertEqual(TestClient(self.client.app).get("/shares", follow_redirects=False).status_code, 303)

        extended = self.client.post(f"/api/shares/{share_id}/extend", json={"seconds": 86400})
        self.assertEqual(extended.status_code, 200)
        self.assertGreater(extended.json()["expires_at"], before)

        guest = TestClient(self.client.app)
        self.assertEqual(guest.post("/share/managed-share/login", data={"password": "old-pass"}, follow_redirects=False).status_code, 303)
        self.assertEqual(guest.get("/share/managed-share/api/transcript").status_code, 200)
        reset = self.client.put(f"/api/shares/{share_id}/password", json={"password": "new-pass"})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(guest.get("/share/managed-share/api/transcript").status_code, 401)
        self.assertEqual(guest.post("/share/managed-share/login", data={"password": "new-pass"}, follow_redirects=False).status_code, 303)

        self.assertEqual(self.client.post(f"/api/shares/{share_id}/stop").status_code, 200)
        shares = self.client.get("/api/shares").json()["shares"]
        managed = next(item for item in shares if item["id"] == share_id)
        self.assertEqual(managed["status"], "stopped")
        self.assertEqual(self.client.delete(f"/api/shares/{share_id}").status_code, 200)
        self.assertFalse(any(item["id"] == share_id for item in self.client.get("/api/shares").json()["shares"]))
        self.assertEqual(guest.get("/share/managed-share/api/transcript").status_code, 404)

    def test_rejects_unknown_or_malformed_pane(self):
        self.login()
        self.assertEqual(self.client.get("/api/panes/%2599/capture").status_code, 404)
        self.assertEqual(self.client.post("/api/panes/not-a-pane/input", json={"text": "x"}).status_code, 404)

    def test_rejects_empty_and_oversized_input(self):
        self.login()
        self.assertEqual(
            self.client.post("/api/panes/%254/input", json={"text": ""}).status_code, 422
        )
        self.assertEqual(
            self.client.post("/api/panes/%254/input", json={"text": "x" * 100001}).status_code,
            422,
        )

    def test_websocket_streams_initial_content(self):
        self.login()
        with self.client.websocket_connect("/ws/panes/%254") as websocket:
            payload = websocket.receive_json()
        self.assertEqual(payload["type"], "capture")
        self.assertIn("\x1b[32mhello", payload["content"])

    def test_the_window_size_is_restored_after_the_last_client_leaves(self):
        """A phone attach at 47 columns must not leave the pane at 47 for everyone else."""
        import tmux_web

        calls = []
        session = tmux_web.TerminalSession.__new__(tmux_web.TerminalSession)
        session.pane_id = "%9"
        session.previous_geometry = "179x51"
        session.window_size_mode = "latest"

        def fake(*args):
            calls.append(args)
            return "" if args[0] == "list-clients" else ""

        session._tmux_value = fake
        session.restore_geometry()
        self.assertIn(("resize-window", "-t", "%9", "-x", "179", "-y", "51"), calls)
        # resize-window forces window-size=manual; putting the mode back is what lets the
        # window follow its clients again. Only this window is touched.
        self.assertIn(("set-option", "-w", "-t", "%9", "window-size", "latest"), calls)
        self.assertFalse([call for call in calls if "default-size" in call])
        self.assertEqual(len(calls), 3)

        # someone still watching: leave their size alone
        calls.clear()

        def watching(*args):
            calls.append(args)
            return "." if args[0] == "list-clients" else ""

        session._tmux_value = watching
        session.restore_geometry()
        self.assertEqual(calls, [("list-clients", "-t", "%9", "-F", ".")])

    def test_a_pty_resize_also_signals_the_child(self):
        """The child inherits the pty, so the kernel sends it no SIGWINCH: tmux kept its old
        size and never repainted, which is why resizing a phone terminal did nothing."""
        import fcntl, pty, signal, struct, termios
        from unittest import mock
        import tmux_web

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)

        class Process:
            pid = 4242
            def poll(self):
                return None

        session = tmux_web.TerminalSession.__new__(tmux_web.TerminalSession)
        session.master_fd = master
        session.process = Process()
        kills = []
        with mock.patch("os.kill", side_effect=lambda pid, sig: kills.append((pid, sig))):
            session._set_size(120, 40)
        self.assertEqual(kills, [(4242, signal.SIGWINCH)])
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(master, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        self.assertEqual((rows, cols), (40, 120))

    def test_a_failed_attach_closes_both_pty_fds(self):
        """Popen raising (EMFILE under fd pressure) must not leak the fresh pty pair."""
        import tmux_web
        from unittest import mock

        opened = []
        real_openpty = os.openpty

        def record_openpty():
            pair = real_openpty()
            opened.append(pair)
            return pair

        with mock.patch.object(tmux_web.TerminalSession, "_tmux_value", return_value=""), \
                mock.patch("os.openpty", side_effect=record_openpty), \
                mock.patch("subprocess.Popen", side_effect=OSError(24, "Too many open files")):
            with self.assertRaises(OSError):
                tmux_web.TerminalSession("%9", 80, 24)
        self.assertEqual(len(opened), 1)
        master, slave = opened[0]
        try:
            with self.assertRaises(OSError):
                os.fstat(master)
            with self.assertRaises(OSError):
                os.fstat(slave)
        finally:
            for fd in (master, slave):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_terminal_websocket_is_bidirectional_and_resizable(self):
        self.login()
        with self.client.websocket_connect("/ws/terminal/%254?cols=100&rows=30") as websocket:
            self.assertEqual(websocket.receive_bytes(), b"\x1b[32mTERM_READY\x1b[0m")
            websocket.send_text(json.dumps({"type": "input", "data": "hello\r"}))
            websocket.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
            # 显式关闭：TestClient 的 __exit__ 在某些 starlette 版本上不会把 disconnect
            # 交给服务端，处理器会一直等 receive_text，session 也就永远不 close
            websocket.close()
            session = self.terminals.sessions[0]
            # Keep the TestClient portal alive while the server handles the disconnect.
            deadline = time.monotonic() + 2
            while not session.closed and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(session.pane_id, "%4")
        self.assertEqual(session.inputs, [b"hello\r"])
        self.assertEqual(session.resizes, [(120, 40)])
        self.assertTrue(session.closed)

    def test_index_is_served(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = self.console_surface()
        self.assertIn("Tmux Chat", page)
        self.assertIn("pane-list", page)
        self.assertIn("composer-card", page)
        self.assertIn("terminal-mode", page)
        self.assertIn("capture-output", page)
        self.assertNotIn(".terminal{display:block;min-width:max-content", page)
        self.assertIn("connectTranscript(id,true)", page)
        self.assertIn("ansi_to_html", page)
        self.assertIn("renderTableLine", page)
        self.assertIn("terminalWidth", page)
        self.assertIn(".terminal-cell.wide{width:2ch}", page)
        self.assertIn(".terminal-table-line{display:inline;white-space:nowrap}", page)
        self.assertNotIn(".terminal-table-line{display:block;height:1.5em", page)
        self.assertIn("visibilitychange", page)
        self.assertIn("connectTranscript", page)
        self.assertIn("marked.parse", page)
        self.assertIn('id="fullscreen"', page)
        self.assertIn('<main class="main" id="main">', page)
        self.assertIn("requestFullscreen", page)
        self.assertIn('id="sidebar-close"', page)
        self.assertIn('id="drawer-backdrop"', page)
        self.assertIn("tmux-sidebar-collapsed", page)
        self.assertIn("env(safe-area-inset-bottom)", page)
        self.assertIn("visualViewport", page)
        self.assertIn("mobile-composing", page)
        self.assertIn("setComposing", page)
        self.assertIn("state.mode==='chat'&&!relayView&&canAutoFocusComposer()", page)
        self.assertIn("mode==='chat'&&state.selected&&canAutoFocusComposer()", page)
        self.assertIn("viewport-fit=cover", page)
        self.assertIn('rel="manifest"', page)
        self.assertIn("apple-mobile-web-app-capable", page)
        self.assertIn("serviceWorker.register('/sw.js')", page)
        self.assertIn("-webkit-text-size-adjust:100%", page)
        self.assertIn(".chat-message.assistant{justify-content:flex-start}", page)
        self.assertIn("background:var(--card);color:var(--ink);font-size:15px", page)
        self.assertIn("refreshTranscript(id,{force=false,refresh=false}", page)
        self.assertIn("state.selected!==id||state.mode!=='chat'", page)
        self.assertIn("/api/hosts'", page)
        for marker in ('id="live-toggle"', "CAPTURE_PAINT_MS", "CAPTURE_MAX_LINES", "pushTerminal",
                       "TERM_BACKLOG_MAX", "queueCapture", "pauseLive", "resumeLive"):
            self.assertIn(marker, page)
        self.assertIn("treeDetails", page)
        self.assertIn("treeOpen:new Set()", page)
        self.assertIn("tmux-tree-open", page)
        self.assertIn("(forceOpen||state.treeOpen.has(key))?' open':''", page)
        self.assertIn("tree-host", page)
        self.assertIn("e.isComposing||isMobile()", page)
        self.assertIn('id="terminal-keys"', page)
        self.assertIn('data-key="ArrowUp"', page)
        self.assertIn("scheduleTerminalFit", page)
        self.assertIn("visualViewport.addEventListener('resize',handleVisualViewportChange)", page)
        self.assertIn("pendingMessages:new Map()", page)
        self.assertIn("addOptimisticMessage", page)
        self.assertIn("const baseline=waiting[0][1].baselineIds;", page)
        self.assertIn("const fresh=messages.filter(message=>message.role==='user'&&!baseline.has(String(message.id)))", page)
        self.assertIn("messageTextKey(message.text)===key", page)
        self.assertIn("function pendingAnchorIndex(messages,pending)", page)
        self.assertIn("pendingAnchorIndex(formal,message)", page)
        self.assertIn('id="relay-receipts"', page)
        self.assertIn("发送中", page)
        self.assertIn("已发送 ✓", page)
        self.assertIn("发送失败", page)
        self.assertIn("pending-wave", page)
        self.assertIn("pane-state", page)
        self.assertIn("refreshPaneStates", page)
        self.assertIn("/api/panes/status", page)
        self.assertIn("applyPaneStates", page)
        self.assertIn("p.capabilities&&p.capabilities.share", page)
        self.assertIn('id="file-panel"', page)
        self.assertIn('id="files-toggle"', page)
        self.assertIn('id="file-input"', page)
        self.assertIn("setupFiles", page)
        self.assertIn("openFiles", page)
        self.assertIn("uploadFiles", page)
        self.assertIn("/files/download?path=", page)
        self.assertIn("dataTransfer.files", page)
        self.assertIn(".file-panel[hidden],.pane-list[hidden]{display:none!important}", page)
        self.assertIn('id="file-close"', page)
        self.assertIn("tmux-files-mode", page)
        self.assertIn("transcriptSignature", page)
        self.assertIn("signature!==state.transcriptSig", page)

        # the UI uses one inline SVG sprite instead of ad-hoc text glyphs and emoji
        for symbol in ("i-folder", "i-server", "i-share", "i-external", "i-refresh", "i-chevron-left",
                       "i-menu", "i-upload", "i-close", "i-file", "i-link", "i-parent"):
            self.assertIn(f'<symbol id="{symbol}"', page, symbol)
        for glyph in ("▤", "⌁", "⇧", "↻", "☰", "‹", "⤒", "✕", "⌘", "↰", "⛶", "📁", "📄", "🔗"):
            self.assertNotIn(glyph, page, glyph)
        self.assertIn(".ico{", page)
        self.assertIn('aria-label="文件"', page)

        # the file panel is scoped to one pane: switching pane/host drops the old
        # listing instead of reloading the previous path on the new machine
        self.assertIn("filesPane:null", page)
        self.assertIn("state.filesPane&&state.filesPane!==id", page)
        self.assertIn("state.filesPane===state.selected?state.filesPath:null", page)
        # 目录列表请求带序号：后发的赢，快速切目录不会被慢响应覆盖
        self.assertIn("pane!==state.selected||seq!==state.filesSeq)return", page)
        # the path bar names the machine the listing came from
        self.assertIn('id="file-host"', page)

        # creating a tmux session on a host that has none
        self.assertIn('id="session-dialog"', page)
        self.assertIn('data-new-session="', page)
        self.assertIn('<symbol id="i-plus"', page)
        self.assertIn("async function submitSessionForm(", page)
        self.assertIn("api('/api/sessions'", page)
        # 关闭窗格：顶栏入口 + 二次确认 + 打 DELETE
        self.assertIn('id="pane-close"', page)
        self.assertIn('id="pane-close-dialog"', page)
        self.assertIn("关闭这个窗格？", page)
        self.assertIn("`/api/panes/${encodeURIComponent(id)}`,{method:'DELETE'}", page)
        self.assertIn("e.stopPropagation();state.sessionHost=add.dataset.newSession", page)

        # per-message copy + per-block controls (code / mermaid / table)
        self.assertIn('data-msg-copy=', page)

        # switching panes must reuse the cached transcript instead of reloading
        self.assertIn("function paintCachedTranscript(", page)
        self.assertIn("function rememberTranscript(", page)
        self.assertIn("function savePaneUi(", page)
        self.assertIn("transcripts:new Map()", page)
        self.assertIn("drafts:new Map()", page)
        self.assertIn("更新中…", page)
        self.assertIn('复制全部', page)
        self.assertIn("async function copyText(", page)
        self.assertIn("function enhanceBlocks(", page)
        self.assertIn("data-block-copy", page)
        self.assertIn("data-block-source", page)
        self.assertIn("/assets/mermaid/mermaid.min.js", page)
        mermaid = self.client.get("/assets/mermaid/mermaid.min.js")
        self.assertEqual(mermaid.status_code, 200)
        self.assertGreater(len(mermaid.content), 100000)
        self.assertEqual(self.client.get("/assets/mermaid/other.js").status_code, 404)
        self.assertIn("$('file-host').textContent=owner?owner.name", page)

        # editable path bar + multi-select zip download
        self.assertIn('id="file-path" type="text"', page)
        self.assertIn("$('file-path').addEventListener('keydown'", page)
        self.assertIn("$('file-path').addEventListener('focus'", page)
        # selection follows desktop conventions instead of a mode toggle
        self.assertNotIn('id="file-select"', page)
        self.assertNotIn("filesSelect", page)
        self.assertIn("e.ctrlKey||e.metaKey", page)
        self.assertIn("e.shiftKey", page)
        self.assertIn("function selectRange(", page)
        self.assertIn("function togglePick(", page)
        self.assertIn('class="file-check"', page.replace("${", ""))
        self.assertIn('<symbol id="i-select"', page)
        self.assertIn("async function downloadArchive(", page)
        # the toolbar "up" button was redundant with the ".." row; it is a batch
        # download button now, and both zip entry points share one code path
        self.assertNotIn('id="file-up"', page)
        self.assertIn('id="file-download"', page)
        self.assertIn('<symbol id="i-download"', page)
        self.assertIn("$('file-download').onclick", page)
        # definition + footer button + toolbar button (two call sites) + 右键/长按菜单
        self.assertEqual(page.count("downloadArchive("), 5)
        self.assertIn("/files/zip", page)
        self.assertIn("function syncFileSelection()", page)

        hosts_page = self.client.get("/hosts")
        self.assertIn("function esc(value)", hosts_page.text)
        self.assertIn("${esc(h.name)}", hosts_page.text)
        # the host key is pinned on first contact: no confirmation checkbox, and the
        # password is masked until the eye is pressed
        self.assertNotIn("confirm-host-key", hosts_page.text)
        self.assertIn("async function ensureScanned(", hosts_page.text)
        self.assertIn('id="fp-note"', hosts_page.text)
        self.assertIn('id="credential-password" type="password"', hosts_page.text)
        self.assertIn('id="toggle-secret"', hosts_page.text)
        self.assertIn("field.type=hidden?'text':'password'", hosts_page.text)
        # the local agent/keys can already be authorised remotely: no credential needed
        self.assertIn('<option value="agent">', hosts_page.text)
        self.assertIn('id="credential-hint"', hosts_page.text)
        self.assertIn("(authMode()!=='agent'&&!field)", hosts_page.text)
        # the payload must carry the credential value itself; `fields.credential.value`
        # sent undefined and made every save/test fail with 422
        self.assertIn("credential:fields.credential,jump_host_id:", hosts_page.text)
        # the add form and the edit dialog both offer a jump host
        self.assertIn('id="jump-host"', hosts_page.text)
        self.assertIn('id="edit-jump-host"', hosts_page.text)
        self.assertNotIn("credential:fields.credential.value", hosts_page.text)
        self.assertIn("h.auth_type==='agent'", hosts_page.text)

        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["display"], "standalone")
        worker = self.client.get("/sw.js")
        self.assertEqual(worker.status_code, 200)
        self.assertIn("no-store", worker.text)

        purposes = {(icon["src"], icon["purpose"]) for icon in manifest.json()["icons"]}
        self.assertIn(("/assets/app-icon-maskable-512.png", "maskable"), purposes)
        self.assertIn(("/assets/app-icon-512.png", "any"), purposes)
        for path in ("/assets/app-icon.svg", "/assets/app-icon-32.png", "/assets/app-icon-180.png",
                     "/assets/app-icon-192.png", "/assets/app-icon-512.png",
                     "/assets/app-icon-maskable-192.png", "/assets/app-icon-maskable-512.png"):
            icon = self.client.get(path)
            self.assertEqual(icon.status_code, 200, path)
            self.assertGreater(len(icon.content), 400, path)
        self.assertEqual(self.client.get("/assets/app-icon-64.png").status_code, 404)
        self.assertEqual(self.client.get("/assets/app-icon-maskable-64.png").status_code, 404)

        # every page carries the favicon, not just the console
        for page in ("/", "/login", "/hosts", "/shares"):
            self.assertIn("app-icon.svg", self.client.get(page).text, page)

    def test_version_endpoint_reports_the_booted_server_version_without_caching(self):
        # 借 Open WebUI 的版本轮询：控制台定期问一次服务端版本，发版后自己发现。
        self.login()
        response = self.client.get("/api/version")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(tmux_web.SERVER_VERSION, "the server must report a version")
        self.assertEqual(response.json(), {"version": tmux_web.SERVER_VERSION, "release": tmux_web.RELEASE_VERSION})
        cache = response.headers.get("cache-control", "")
        self.assertIn("no-cache", cache)
        self.assertIn("no-store", cache)

    def test_skill_catalog_uses_the_pane_host(self):
        from unittest.mock import patch
        self.assertEqual(self.client.get("/api/panes/%254/skills").status_code, 401)
        self.login()
        host = self.remote_store.add_host(name="remote", credential="", hostname="test", username="tester")
        host_id = host["id"]
        with patch.object(FakeSSHRunner, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=b'["remote-only"]')) as run:
            result = self.client.get(f"/api/panes/{host_id}~%259/skills")
            self.assertEqual(result.json(), {"provider": "claude", "skills": ["remote-only"]})
            self.assertIn("claude /srv/work", run.call_args.args[0])
            self.assertIn(b"def catalog(", run.call_args.kwargs["input_bytes"])
            self.client.get(f"/api/panes/{host_id}~%259/skills")
            self.assertEqual(run.call_count, 1)

    def test_remote_skill_catalog_reuses_fresh_host_listing(self):
        from unittest import mock
        self.login()
        host = self.remote_store.add_host(name="remote", credential="", hostname="test", username="tester",
                                          port=22, auth_type="agent", fingerprint="test-fingerprint")
        self.assertEqual(self.client.get("/api/hosts?refresh=1").status_code, 200)
        with mock.patch.object(FakeSSHRunner, "list_panes", side_effect=AssertionError("duplicate listing")), \
                mock.patch.object(FakeSSHRunner, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=b'["managed"]')):
            result = self.client.get(f"/api/panes/{host['id']}~%259/skills")
        self.assertEqual(result.json(), {"provider":"claude", "skills":["managed"]})

    def test_skill_read_falls_back_only_after_agent_failure_with_a_pinned_host(self):
        from unittest import mock
        runner = tmux_web.SSHRunner({"id": "node", "host_key": "fixture", "hostname": "unreachable"})
        with mock.patch.object(runner, "run", side_effect=RuntimeError("busy")), \
                mock.patch.object(tmux_web.SSHRunner, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=b'["managed"]')) as ssh:
            for used, pinned in [({"agent", "ssh"}, True), ({"agent"}, False), ({"agent"}, True)]:
                runner.used_transports = used
                with mock.patch.object(runner, "host_key_pinned", return_value=pinned):
                    if used == {"agent"} and pinned:
                        self.assertEqual(tmux_web.remote_skill_names(runner, "codex", "/work"), ["managed"])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "busy"):
                            tmux_web.remote_skill_names(runner, "codex", "/work")
            ssh.assert_called_once()
            self.assertFalse(ssh.call_args.kwargs["resolve_tmux"])
            self.assertIn(b"def catalog(", ssh.call_args.kwargs["input_bytes"])

    def test_version_endpoint_requires_auth(self):
        self.assertEqual(self.client.get("/api/version").status_code, 401)

    def test_console_page_embeds_the_server_version_as_its_baseline(self):
        self.login()
        page = self.client.get("/").text
        self.assertIn('<meta name="server-version" content="%s">' % tmux_web.SERVER_VERSION, page)
        self.assertNotIn("__SERVER_VERSION__", page)

    def test_version_endpoint_moves_when_the_server_version_moves(self):
        # 端点必须读启动时算好的 live 值：部署拉到新 commit、服务重启后，旧页面
        # 轮询到新串才会弹 toast；写死在代码里的串测不出这点。
        self.login()
        with mock.patch.object(tmux_web, "SERVER_VERSION", "deadbee1234"):
            response = self.client.get("/api/version")
        self.assertEqual(response.json(), {"version": "deadbee1234", "release": tmux_web.RELEASE_VERSION})

    def test_ansi_sequences_can_be_removed_without_changing_columns(self):
        raw = "| A   | B |\n\x1b[38;5;231m| 123 | x |\x1b[0m"
        self.assertEqual(ANSI_RE.sub("", raw), "| A   | B |\n| 123 | x |")

    def test_panes_expose_provider_and_capabilities(self):
        self.login()
        pane = self.client.get("/api/panes").json()["panes"][0]
        self.assertEqual(pane["provider"], "codex")
        self.assertEqual(pane["capabilities"], {"transcript": True, "terminal": True,
                                                "relay": True, "share": True, "native_form":"owner"})
        # a plain shell has no transcript but is still shareable (screen mode)
        # 列表会把 provider 写进 pane 字典（真实后端每次都返回新字典，这里是假后端在复用，
        # 所以要把它一起清掉，否则测的还是上一轮的判定）
        self.tmux.panes[0]["command"] = "zsh"
        self.tmux.panes[0].pop("provider", None)
        pane = self.client.get("/api/panes").json()["panes"][0]
        self.assertFalse(pane["capabilities"]["transcript"])
        self.assertTrue(pane["capabilities"]["share"])

    def test_snapshot_ansi_skips_unchanged_content(self):
        backend = TmuxBackend()
        backend.validate_pane = lambda pane_id: {"id": pane_id, "command": "codex"}
        backend._run = lambda *args, input_bytes=None: "same\n"
        digest, content = backend.snapshot_ansi("%4", 100)
        self.assertEqual(content, "same\n")
        digest_again, content_again = backend.snapshot_ansi("%4", 100, digest)
        self.assertEqual(digest_again, digest)
        self.assertIsNone(content_again)

    def test_api_token_authenticates_without_a_cookie(self):
        with patch.dict("os.environ", {"TMUX_WEB_API_TOKEN": "relay-token-value"}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            self.assertEqual(client.get("/api/panes").status_code, 401)
            self.assertEqual(client.get("/api/panes", headers={"X-TmuxWeb-Token": "wrong"}).status_code, 401)
            self.assertEqual(client.get("/api/panes",
                                        headers={"X-TmuxWeb-Token": "relay-token-value"}).status_code, 200)
            self.assertEqual(client.get("/api/panes", headers={
                "Authorization": "Bearer relay-token-value"}).status_code, 200)

    def test_an_uploaded_image_previews_from_a_remote_pane(self):
        """远端 pane 的上传文件不该走本机 os.path.isfile —— 那会永远 404「图片已不在这里」。"""
        self.login()
        host_key = "10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        host = self.remote_store.add_host(
            name="box", hostname="10.0.0.9", port=22, username="u", auth_type="password",
            credential="pw", transport="ssh", host_key=host_key,
            fingerprint=host_key_fingerprint(host_key))
        host_id = host["id"]
        remote_pane = self.client.get("/api/hosts").json()["hosts"]
        pane = next(p for h in remote_pane for p in (h.get("panes") or []) if h["id"] == host_id)
        pane_id = pane["id"]
        path = pane["path"].rstrip("/") + "/upload_files/shot.png"
        FakeSSHRunner.remote_files[path] = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        response = self.client.get(f"/api/panes/{pane_id}/uploads/shot.png")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content[:4], b"\x89PNG")

    def test_a_renderer_crash_leaves_a_trace_of_what_was_painted(self):
        """Chrome 整页崩溃什么都不会留下：前端每画一批就回一条，日志里能看到最后一条消息。"""
        self.login()
        response = self.client.post("/api/client-trace",
                                    json={"pane": "%443", "last_id": "45102", "rendered": 72,
                                          "note": "full"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        # 排查开关必须在页面脚本里（?safe=1 关后处理、?plain=1 不解析 markdown）
        app_js = self.client.get("/assets/console-app.js").text
        for marker in ("SAFE_MODE", "PLAIN_MODE", "clientTrace("):
            self.assertIn(marker, app_js)

    def test_startup_warms_the_address_cache(self):
        """重启后第一次 /api/fed/ping 不能回空地址：父节点会以为这台机器没有直连地址。

        它只读缓存（不能为探测等 6 秒子进程），所以必须靠启动事件先算一遍。
        """
        import tmux_web

        tmux_web.ADDRESS_CACHE.update({"at": 0.0, "value": []})
        app = create_app(self.tmux, password="unit-test-admin-password",
                         session_secret="test-secret", terminal_factory=self.terminals,
                         share_db=":memory:", queue_db=":memory:", remote_store=self.remote_store,
                         remote_runner_factory=FakeSSHRunner)
        with TestClient(app):                     # 进 with 才会触发 startup 事件
            self.assertGreater(tmux_web.ADDRESS_CACHE["at"], 0.0, "启动时没预热地址缓存")
            payload = self.client.get("/api/fed/ping").json()
        self.assertTrue(payload["tmuxweb"])

    def test_an_openlist_link_without_d_still_plays(self):
        """openlist 少了 /d/ 会回自己的网页(200 + text/html)，不是文件 —— 代理要补一次。"""
        import tmux_web

        self.assertEqual(tmux_web.openlist_d_link("https://alist.461922950.xyz/189Cloud/videos/x.mp4"),
                         "https://alist.461922950.xyz/d/189Cloud/videos/x.mp4")
        # 已经是对的、或者本来就没有路径：不要瞎补
        self.assertEqual(tmux_web.openlist_d_link("https://h/d/189Cloud/videos/x.mp4"), "")
        self.assertEqual(tmux_web.openlist_d_link("https://h/"), "")
        # 查询串要原样留着（签名链接靠它）
        self.assertEqual(tmux_web.openlist_d_link("https://h/a/x.mp4?token=1"),
                         "https://h/d/a/x.mp4?token=1")

    def isolated_console(self, **kwargs):
        """A client with every SQLite store in memory: the live service holds home.db."""
        tmux = kwargs.pop("tmux", None) or self.tmux
        client = TestClient(create_app(
            tmux, password="unit-test-admin-password", session_secret="test-secret",
            terminal_factory=self.terminals, share_db=":memory:", queue_db=":memory:",
            home_db=":memory:", remote_store=self.remote_store,
            remote_runner_factory=FakeSSHRunner, **kwargs,
        ))
        client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        return client

    def test_double_clicking_the_chat_title_names_the_pane(self):
        """The chat name in the topbar is editable: dblclick opens the rename dialog."""
        client = self.isolated_console()
        page = client.get("/").text
        self.assertIn('id="selected-title"', page)
        app = client.get("/assets/console-app.js").text
        self.assertIn("wireChatNameRename();", app)
        self.assertIn("title.addEventListener('dblclick'", app)
        self.assertIn("/name`", app)
        self.assertIn("pane.custom_name", app)
        css = client.get("/assets/console.css").text
        self.assertIn("#selected-title.renameable", css)

    def test_a_pane_name_is_stored_separately_from_the_title_and_window(self):
        """Naming a pane must not fight the program's pane_title or the window name."""
        client = self.isolated_console()
        named = client.post("/api/panes/%4/name", json={"name": "发布验收"})
        self.assertEqual(named.status_code, 200)
        self.assertEqual(named.json()["name"], "发布验收")
        self.assertEqual(self.tmux.named_panes[-1], ("%4", "发布验收"))

        panes = client.get("/api/panes").json()["panes"]
        target = next(pane for pane in panes if pane["id"] == "%4")
        self.assertEqual(target["custom_name"], "发布验收")
        # the window name and the auto title are untouched
        self.assertEqual(target["title"], "Review | repo")
        self.assertEqual(target["window_name"], "editor")

        # an empty name restores the default (the option is unset)
        cleared = client.post("/api/panes/%4/name", json={"name": "   "})
        self.assertEqual(cleared.json()["name"], "")
        panes = client.get("/api/panes").json()["panes"]
        target = next(pane for pane in panes if pane["id"] == "%4")
        self.assertNotIn("custom_name", target)

    def test_a_too_long_pane_name_is_refused(self):
        client = self.isolated_console()
        response = client.post("/api/panes/%4/name", json={"name": "x" * 81})
        self.assertEqual(response.status_code, 422)

    def test_the_queue_strip_is_wired_into_the_composer(self):
        """忙的时候消息排到服务端队列，输入框上方的条带能看能撤能改成"打断并立刻发"。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn('id="queue-strip"', page)
        app = self.client.get("/assets/console-app.js").text
        for marker in ("function enqueueMessage(", "async function loadQueue()",
                       "function renderQueueStrip(", "function setupQueueStrip(",
                       "setQueueMode(", "/mode", "data-queue-cancel", "data-queue-steer",
                       "window.TmuxQueue"):
            self.assertIn(marker, app)
        # 忙的时候走排队，而不是硬塞
        self.assertIn("if(!prompt&&state.waiting){", app)
        self.assertIn("enqueueMessage(text)", app)
        css = self.client.get("/assets/console.css").text
        self.assertIn(".queue-strip{", css)
        self.assertIn(".qs-text{max-width", css)

    def test_a_terminal_key_is_sent_exactly_once(self):
        """#terminal-ext 嵌在 #terminal-keys 里：两层各挂一个 pointerdown 就会一次点击发两遍。"""
        self.login()
        app = self.client.get("/assets/console-app.js").text
        self.assertEqual(app.count("sendTerminalKey(button.dataset.key)"), 1,
                         "按键只能有一处出口")
        self.assertNotIn("$('terminal-ext')?.addEventListener('pointerdown'", app,
                         "扩展键盘的按键由 #terminal-keys 的委托处理器覆盖，别再挂第二个")
        # 删掉第二个处理器时别忘了它独有的 paste 分支
        self.assertIn("}else if(button.dataset.paste){", app)
        self.assertIn("pasteIntoTerminal();", app)

    def test_the_chat_has_an_in_conversation_search(self):
        """顶栏放大镜：命中高亮、1/N 计数、上下跳、Esc 关闭，重渲染后自动补盖。"""
        self.login()
        page = self.client.get("/").text
        for marker in ('id="chat-search-button"', 'id="chat-search"', 'id="cs-input"',
                       'id="cs-count"', 'id="cs-prev"', 'id="cs-next"', 'id="cs-close"'):
            self.assertIn(marker, page)
        module = self.client.get("/assets/chat-search.js").text
        for marker in ("createTreeWalker", "MutationObserver", "new Highlight(", "'f'"):
            self.assertIn(marker, module)
        self.assertNotIn("replaceChild", module)
        app = self.client.get("/assets/console-app.js").text
        self.assertIn("TmuxChatSearch.mount()", app)
        self.assertIn("searchBtn=$('chat-search-button')", app)
        self.assertIn("searchBtn.disabled=false", app)
        self.assertIn("TmuxChatSearch.close()", app, "切走时要收掉搜索条和高亮")
        css = self.client.get("/assets/console.css").text
        self.assertIn("::highlight(cs-current)", css)

    def test_the_header_subtitle_never_prints_undefined(self):
        """#212 的老毛病：缓存里的 pane 少字段，顶栏就印 "undefined · undefined · /path"。"""
        self.login()
        app = self.client.get("/assets/console-app.js").text
        self.assertIn("function paneHeaderParts(p)", app)
        self.assertIn("paintPaneHeader(p);", app)
        self.assertNotIn("${p.target} · ${p.command} · ${p.path}", app,
                         "直接拼字段就是 undefined 的来源")
        self.assertIn("function repaintSelectedHeader()", app,
                      "hosts 刷新后要把顶栏对齐真数据，不能停在缓存画的值上")
        # pane-cache.js 没有单独路由，只在 bundle 里
        cache = self.client.get("/assets/console.bundle.js").text
        self.assertIn("target: pane.target || '', command: pane.command || ''", cache,
                      "缓存要存顶栏要用的字段")
        self.assertIn("形状不对就当没有缓存", cache,
                      "旧形状缓存要作废，而不是先画出半份数据")

    def test_the_header_never_lets_the_buttons_cover_the_menu_button(self):
        """手机窄屏：左边「菜单 + 标题」不能被右边那排按钮压住；右边自己横向滚。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn('<div class="topbar-lead">', page)
        self.assertNotIn('<div style="display:flex;align-items:center;min-width:0">', page,
                         "内联样式那段正是被压住的元凶")
        css = self.client.get("/assets/console.css").text
        self.assertIn(".topbar>.topbar-lead{display:flex;align-items:center;min-width:0;flex:0 1 auto;overflow:hidden}", css)
        self.assertIn(".top-actions{flex:0 1 auto;min-width:0;overflow-x:auto", css)
        self.assertIn(".top-actions.scrolls{box-shadow:inset 12px 0 14px -9px", css)
        self.assertIn(".top-actions>*{flex:0 0 auto}", css, "按钮本身不许被压扁")
        app = self.client.get("/assets/console-app.js").text
        self.assertIn("acts.classList.toggle('scrolls',acts.scrollWidth>acts.clientWidth+1)", app)

    def test_the_git_panel_is_wired_into_the_file_bar(self):
        """Git 面板：文件栏上的入口、面板骨架、以及按钮到接口的连线。"""
        self.login()
        page = self.client.get("/").text
        for marker in ('id="git-open"', 'id="git-panel"', 'id="gp-checkpoints"',
                       'id="gp-diff-wrap"', 'id="gp-worktree"', 'href="#i-git"'):
            self.assertIn(marker, page)
        app = self.client.get("/assets/console-app.js").text
        for marker in ("function openGitPanel(", "async function gpCheckpoint(",
                       "async function gpRestore(", "async function gpDiff(",
                       "gitUrl('/checkpoint')", "gitUrl('/restore')", "gitUrl('/worktree')",
                       "function gitUrl(suffix)",
                       "headers:{'Content-Type':'application/json'}"):
            self.assertIn(marker, app)
        css = self.client.get("/assets/console.css").text    # 页面加载的是这一份（bundled）
        self.assertIn(".git-panel{", css)
        self.assertIn(".gp-path{flex:1;min-width:0;overflow:hidden", css)

    def test_the_media_proxy_refuses_to_touch_the_local_network(self):
        """远端视频要代取（octet-stream + attachment 浏览器不播），但不能变成打内网的后门。"""
        self.login()
        for bad in ("http://127.0.0.1:8787/api/hosts", "http://10.0.0.5/x.mp4",
                    "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                    "http://user:pass@example.com/x.mp4"):
            response = self.client.get("/api/media/proxy", params={"url": bad})
            self.assertEqual(response.status_code, 400, f"{bad} 应该被拒绝：{response.text}")

    def test_a_remote_video_can_be_range_read(self):
        """视频拖进度条要 206 + Content-Range；远端不按区间读的话，浏览器得先下完整个文件。"""
        self.login()
        host_key = "10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        host = self.remote_store.add_host(
            name="box", hostname="10.0.0.9", port=22, username="u", auth_type="password",
            credential="pw", transport="ssh", host_key=host_key,
            fingerprint=host_key_fingerprint(host_key))
        hosts = self.client.get("/api/hosts").json()["hosts"]
        pane = next(p for h in hosts for p in (h.get("panes") or []) if h["id"] == host["id"])
        path = pane["path"].rstrip("/") + "/lamp_bird.mp4"
        FakeSSHRunner.remote_files[path] = b"0123456789" * 100

        response = self.client.get(f"/api/panes/{pane['id']}/files/raw", params={"path": path},
                                   headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.content, b"0123456789")
        self.assertEqual(response.headers["content-range"], "bytes 10-19/1000")

        tail = self.client.get(f"/api/panes/{pane['id']}/files/raw", params={"path": path},
                               headers={"Range": "bytes=-8"})
        self.assertEqual(tail.status_code, 206)
        self.assertEqual(len(tail.content), 8)

    def test_virtual_machine_nat_addresses_are_never_listed(self):
        """10.0.2.0/24 是 QEMU/VirtualBox 的 NAT：只有虚拟机自己看得见，列出来就是让人去等超时。"""
        self.login()
        patcher, _ = self._patch_addresses()
        with patcher:
            payload = self.client.get("/api/system/addresses").json()
        urls = [item["url"] for item in payload["addresses"]]
        self.assertNotIn("http://10.0.2.12:8787", urls)
        self.assertIn("http://10.168.236.31:8787", urls, "真实局域网地址不能被误杀")

    def test_the_pause_button_is_hidden_until_the_pref_turns_it_on(self):
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
        self.assertIn('id="live-toggle" hidden', html, "暂停按钮默认该藏起来")
        app = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-app.js").read_text()
        self.assertIn("applyPauseButton", app)
        # .share-btn 是 display:inline-grid，光有 hidden 属性压不住，按钮会照旧显示
        base = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-base.css").read_text()
        self.assertIn(".share-btn[hidden]", base)

    def test_sidebar_search_reaches_later_panes_and_matches_only_in_chat(self):
        self.login()
        original = self.tmux.panes[0]
        self.tmux.panes = [dict(original, id=f"%{i}", pane_pid=str(i+1000), provider="hermes")
                           for i in range(405, 443)]
        calls = []
        def transcript(pid, limit, pane):
            calls.append((pid, limit))
            text = "Install MCPHub <example>" if pid == "%442" else "nothing here"
            return {"messages": [{"text": text}, *[{"text": "later"} for _ in range(450)]]}
        self.tmux.transcript = transcript
        path = "/api/search/pane-content"
        panes = ["%405", "%442"]
        body = [{"pane": pid, "terms": ["mcphub"]} for pid in panes]
        with mock.patch.object(self.tmux, "list_panes", side_effect=AssertionError("cache check contacted tmux")):
            cold = self.client.post(path+"?cached_only=1", json=body)
        self.assertTrue(all("next" in row for row in cold.json()["results"]))
        self.assertEqual(calls, [])
        for q in ["mcphub", "MCPHUB"]:
            found = self.client.post(path, json=[{"pane": pid, "terms": [q]} for pid in panes])
            self.assertEqual(found.status_code, 200)
            miss, hit = found.json()["results"]
            self.assertIsNone(miss["snippet"])
            self.assertEqual((hit["pane"], hit["source"], hit["snippet"]), ("%442", "transcript", "Install MCPHub <example>"))
        warm = self.client.post(path+"?cached_only=1", json=body).json()["results"]
        self.assertEqual([row["routes"] for row in warm], [["server_cache"], ["server_cache"]])
        self.assertIsNone(warm[0]["snippet"])
        self.assertIn("MCPHub", warm[1]["snippet"])
        self.assertCountEqual(calls, [(pid, 500) for pid in panes])
        self.assertEqual(self.tmux.keys, [])
        self.assertEqual(self.client.post(path, json=[{"pane": "%4", "terms":["a"]}]*9).status_code, 422)

    def test_concurrent_sidebar_searches_share_a_transcript_read(self):
        from concurrent.futures import ThreadPoolExecutor
        self.tmux.panes[0].update(provider="hermes")
        reading = threading.Event()
        release = threading.Event()

        def transcript(*args, **kwargs):
            reading.set()
            if not release.wait(10):
                raise RuntimeError("fixture timed out")
            return {"messages": [{"text": "MCPHub setup"}]}

        self.tmux.transcript = mock.Mock(side_effect=transcript)
        with self.client as client, ThreadPoolExecutor(max_workers=2) as pool:
            self.login()
            client.get("/api/hosts")
            def query(term):
                return client.post("/api/search/pane-content", json=[{"pane": "%4", "terms": [term]}])
            first = pool.submit(query, "mcphub")
            try:
                self.assertTrue(reading.wait(10))
                second = pool.submit(query, "setup")
            finally:
                release.set()
            for request in (first, second):
                self.assertEqual(request.result().json()["results"][0]["snippet"], "MCPHub setup")
        self.tmux.transcript.assert_called_once()

    def test_sidebar_search_handles_shells_unavailable_panes_and_requires_login(self):
        path = "/api/search/pane-content"
        body = [{"pane": "%4", "terms": ["hub"]}, {"pane": "%999", "terms": ["hub"]}]
        self.assertEqual(self.client.post(path, json=body).status_code, 401)
        self.login()
        self.tmux.panes[0].update(provider="", command="sh")
        self.tmux.output = "only in terminal: MCPHub"
        found, missing = self.client.post(path, json=body).json()["results"]
        self.assertEqual(found["source"], "screen")
        self.assertIn("MCPHub", found["snippet"])
        self.assertTrue(missing["unavailable"])

    def test_the_sidebar_has_a_search_box_that_filters_the_pane_tree(self):
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
        # the input lives in the header row, right of 会话, between the label
        # and the expand/collapse buttons — no own row, no custom × button
        tools_start = html.index('<div class="tree-tools">')
        tools = html[tools_start:html.index('</div>', tools_start)]
        self.assertIn('id="pane-search"', tools)
        self.assertIn('placeholder="搜索…"', tools, "窄侧边栏里 placeholder 要短，完整提示放 title")
        self.assertIn('title="搜索会话信息、最近 500 条聊天；无聊天记录时查最近 200 行终端"', tools)
        self.assertNotIn('id="pane-search-count"', tools, "命中数不在标题行里，单独放树列表上方")
        self.assertIn('id="pane-search-count"', html, "命中数指示保留")
        self.assertNotIn('pane-search-clear', html, "自定义 × 已去掉：原生 search 清除 + Esc 才是清空路径")
        self.assertNotIn('tree-search-clear', html)
        self.assertNotIn('class="tree-search"', html)
        self.assertIn('.tree-tools input{', html, "搜索框沿用侧边栏既有样式")
        self.assertNotIn('.tree-search input{', html)
        self.assertIn('.tree-empty{', html, "无命中时显示空状态")
        app = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-app.js").read_text()
        for marker in ("parsePaneSearchTerms", "paneMatchesSearch", "filterHostsForSearch",
                       "paneSearchQuery", "没有匹配的会话",
                       "$('pane-search').addEventListener('input'",
                       "event.stopPropagation();clearPaneSearch()"):
            self.assertIn(marker, app)
        self.assertNotIn("syncPaneSearchClear", app, "× 按钮连同它的 handler 一起删掉")
        self.assertNotIn("pane-search-clear", app)
        # searching force-opens the matching ancestors at render time only;
        # it must not write state.treeOpen, or clearing would lose the user's
        # manual expand/collapse state
        self.assertIn("forceOpen=false", app)
        self.assertIn(",searching);}).join('');", app)
        base = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-base.css").read_text()
        self.assertIn(".tree-tools-label{flex:none", base, "标签固定宽度，输入框才能吃掉标题行剩余空间")
        self.assertNotIn(".tree-tools-label{flex:1", base)

    def test_off_screen_messages_are_not_virtualized_with_content_visibility(self):
        """content-visibility:auto 曾在 WebKit 上留白屏、在 Windows Chrome 上整页崩。

        #378 侧边栏点一下就 STATUS_BREAKPOINT（2026-11-20），`?nocv=1` 立刻正常，
        所以这条 CSS 必须保持删除状态 —— 想省绘制请换别的办法。
        """
        app = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-app.js").read_text()
        base = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-base.css").read_text()
        self.assertNotIn(".msg-virtualized{content-visibility", base)
        self.assertNotIn("contain-intrinsic-size", base, "整条虚拟化 CSS 都不该回来")
        self.assertNotIn("virtualizedClass", app)
        self.assertIn("before=state.renderedMessages", app)
        self.assertIn("if(next.get(id)===item)continue", app)
        # load-earlier must anchor on the visible message, not a scrollHeight delta.
        self.assertIn("function firstVisibleMessage()", app)
        self.assertIn("function restoreAnchor(anchor)", app)
        self.assertNotIn("scroll.scrollTop+=scroll.scrollHeight-height", app)

    def test_manual_inspection_is_opt_in_and_separate_from_legacy_prompt_card(self):
        self.login()
        prefs = self.client.get("/api/prefs").json()["prefs"]
        self.assertEqual(prefs.get("manual_inspect", "off"), "off")
        for value in ("on", "off"):
            response = self.client.put("/api/prefs", json={"manual_inspect": value, "prompt_card": "on"})
            self.assertEqual(response.status_code, 200)
            prefs = self.client.get("/api/prefs").json()["prefs"]
            self.assertEqual((prefs["manual_inspect"], prefs["prompt_card"]), (value, "off"))
        self.assertEqual(self.client.put("/api/prefs", json={"manual_inspect": "maybe"}).status_code, 400)

    def test_the_pause_button_preference_is_accepted(self):
        self.login()
        response = self.client.put("/api/prefs", json={"pause_button": "on"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["prefs"]["pause_button"], "on")
        self.assertEqual(self.client.put("/api/prefs", json={"pause_button": "maybe"}).status_code, 400)

    def test_input_completion_borrows_open_webui_at_and_slash_without_rich_text(self):
        """Borrowed from Open WebUI MessageInput.svelte (3164 lines, 0.13.3):
        '@' -> AtCommands and '/' -> SlashCommands, rendered by
        getSuggestionRenderer(CommandSuggestionList). Only the interaction
        is borrowed: the textarea stays plain text, completion only deletes
        a token, inserts plain text, or switches pane; sendInput is untouched
        and the text goes to tmux verbatim. No Tiptap, no rich text."""
        page = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
        app = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-app.js").read_text()
        self.assertIn('id="input-complete"', page)
        self.assertIn("@ 提及会话 · / 技能和提示词", page)
        self.assertIn(".input-complete{", page)
        self.assertIn("function matchInputCompletion(", app)
        self.assertIn("function filterPaneCompletions(", app)
        self.assertIn("function filterCommandCompletions(", app)
        self.assertIn("function applyCompletion(", app)
        self.assertIn("function acceptInputComplete(", app)
        self.assertIn("function inputCommands(){", app)
        self.assertIn("data-msg-resend", app)
        # Red line: no rich-text editor anywhere in the console bundle.
        self.assertNotIn("@tiptap", app)
        self.assertNotIn("RichTextInput", app)
        # Accepting a completion must never send: no send path in the
        # accept function, and no fetch/api call either.
        start = app.index("  function acceptInputComplete(")
        end = app.index("\n  async function createShare(", start)
        self.assertNotIn("sendInput(", app[start:end])
        self.assertNotIn("api(", app[start:end])
        # The resend button only refills the input; it never sends by itself.
        self.assertIn("input.dispatchEvent(new Event('input'))", app)
        resend = app.index("data-msg-resend")
        self.assertNotIn("sendInput(", app[resend:resend + 600])

    def test_remote_loading_shows_in_the_status_line_not_a_floating_card(self):
        """加载时 idle/thinking 本来没有意义：状态就画在那一行，别弹浮层。"""
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
        self.assertNotIn('id="load-card"', html, "加载不该再弹浮层")
        app = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-app.js").read_text()
        self.assertIn("子节点加载中", app)
        self.assertIn("loadingActive()", app)

    def test_a_session_and_a_window_can_be_renamed(self):
        self.login()
        response = self.client.post("/api/sessions/rename",
                                    json={"host": "localhost", "session": "codex", "name": "codex2"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["session"], "codex2")
        self.assertIn(("codex", "codex2"), self.tmux.renamed_sessions)

        window = self.client.post("/api/panes/%254/rename", json={"name": "my api"})
        self.assertEqual(window.status_code, 200, window.text)
        self.assertEqual(window.json()["window"], "my api")
        self.assertIn(("%4", "my api"), self.tmux.renamed_windows)

    def test_a_bad_session_name_is_rejected(self):
        self.login()
        response = self.client.post("/api/sessions/rename",
                                    json={"host": "localhost", "session": "work", "name": "bad name!"})
        self.assertEqual(response.status_code, 422)

    def test_choosing_a_pane_closes_the_drawer_with_its_scrim(self):
        """手机上那层灰色遮罩 = .drawer-backdrop.show 没被清掉；选 pane 必须整块收掉。"""
        html = (Path(__file__).resolve().parent.parent / "web" / "assets" / "console-app.js").read_text()
        # 选 pane 时曾经只摘了 open、留下 .drawer-backdrop.show —— 手机上就是一层灰罩
        self.assertNotIn("$('sidebar').classList.remove('open');}", html)
        self.assertIn("closeSidebar();}", html)

    def test_the_sidebar_always_opens_on_the_session_list(self):
        """The file panel is an action, not a remembered preference: it used to be
        restored from localStorage, so a chat opened from the board or the grid could
        start with 文件管理 instead of the server list."""
        self.login()
        page = self.console_surface()
        self.assertIn("id=\"file-panel\"", page)          # 控制台页面本身，不是登录页
        self.assertNotIn("localStorage.getItem('tmux-files-mode')", page)
        self.assertNotIn("localStorage.setItem('tmux-files-mode'", page)
        # 默认（设置里没开「按 pane 记住」）每次加载都从会话列表开始
        self.assertIn("filesMode:false", page)
        # 面板开着时切 pane 仍然跟随
        self.assertIn("else if(state.filesMode){openFiles(null);}", page)
        # 开启记忆后按 pane 读写
        self.assertIn("readFilesView(id)", page)
        self.assertIn("writeFilesView()", page)

    def test_refresh_on_a_local_pane_does_not_500(self):
        """The API passes refresh=1 for every transport; the local one has no cache to bypass."""
        self.login()
        code = self.client.get("/api/panes/%254/transcript?limit=5&refresh=1").status_code
        # 200 or a typed 409 ("no messages") are fine -- a 500 is not
        self.assertNotEqual(code, 500)
        self.assertIn(code, (200, 409))

    def test_api_session_mints_a_cookie_from_the_api_token(self):
        # Browser clients cannot send an Authorization header on a WebSocket
        # handshake, so a token login mints the session cookie here.
        with patch.dict("os.environ", {"TMUX_WEB_API_TOKEN": "relay-token-value"}):
            client = TestClient(create_app(self.tmux, password="pw",
                                           session_secret="test-session-secret",
                                           share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            self.assertEqual(client.post("/api/session").status_code, 401)
            self.assertEqual(client.post("/api/session",
                                         headers={"Authorization": "Bearer wrong"}).status_code, 401)
            response = client.post("/api/session",
                                   headers={"Authorization": "Bearer relay-token-value"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("tmux_web_session", response.headers.get("set-cookie", ""))
            # The minted cookie authenticates without the token header.
            self.assertEqual(client.get("/api/panes").status_code, 200)

    def test_relay_peers_reports_status_for_every_host(self):
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        })
        peers = self.client.get("/api/relay/peers").json()["peers"]
        self.assertEqual(len(peers), 2)
        local = next(item for item in peers if item["host_id"] == "localhost")
        remote = next(item for item in peers if item["host_id"] != "localhost")
        self.assertEqual(local["status"], "idle")
        self.assertEqual(remote["provider"], "claude")
        self.assertEqual(remote["host_name"], "build-box")
        statuses = self.client.get("/api/panes/status").json()["panes"]
        self.assertEqual({item["id"] for item in statuses}, {"%4", remote["id"]})

    def test_sweep_sends_one_batched_call_per_remote_host(self):
        """sweep 每 host 每轮只发一次 capture_and_status_many，不再拆两调用。"""
        class RecordingRunner(FakeSSHRunner):
            calls = []

            def capture_and_status_many(self, pane_ids, history=40, status_ids=None, panes=None):
                RecordingRunner.calls.append(
                    ("capture_and_status_many", tuple(pane_ids), history, tuple(status_ids or ())))
                return ({pane_id: "screen %s" % pane_id for pane_id in pane_ids},
                        {pane_id: "idle" for pane_id in (status_ids or ())})

            def capture_many(self, pane_ids, history=40, panes=None):
                RecordingRunner.calls.append(("capture_many", tuple(pane_ids)))
                return {pane_id: "screen %s" % pane_id for pane_id in pane_ids}

            def status_many(self, pane_ids):
                RecordingRunner.calls.append(("status_many", tuple(pane_ids)))
                return {pane_id: "idle" for pane_id in pane_ids}

        self.client = TestClient(create_app(
            self.tmux, password="unit-test-admin-password",
            session_secret="test-secret", terminal_factory=self.terminals,
            share_db=":memory:", queue_db=":memory:", home_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=RecordingRunner))
        self.login()
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        })
        statuses = self.client.get("/api/panes/status").json()["panes"]
        remote = next(item for item in statuses if "~" in item["id"])
        self.assertEqual(remote["status"], "idle")
        self.assertEqual(remote["status_source"], "session")
        self.assertEqual(RecordingRunner.calls,
                         [("capture_and_status_many", ("%9",), 40, ("%9",))])

    def test_panes_status_answers_from_a_stale_cache_without_waiting_for_the_sweep(self):
        """过期缓存要立刻回旧状态；只有冷缓存才短等，绝不等整轮 sweep。"""
        import time

        class SlowTmux(FakeTmux):
            """抓屏一次睡 3 秒：sweep 本身慢，但 status 不能跟着慢。"""

            def capture_many(self, pane_ids, history=40, panes=None):
                time.sleep(3.0)
                return super().capture_many(pane_ids, history, panes)

        client = TestClient(create_app(
            SlowTmux(),
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:", queue_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))
        response = client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

        # 冷缓存：旧代码要等满 2.0 秒，新代码只短等 0.5 秒
        started = time.monotonic()
        first = client.get("/api/panes/status")
        cold_elapsed = time.monotonic() - started
        self.assertEqual(first.status_code, 200)
        self.assertLess(cold_elapsed, 1.5)

        # 等后台 sweep 跑完，把缓存填上
        states = []
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            states = client.get("/api/panes/status").json()["panes"]
            if states:
                break
            time.sleep(0.2)
        self.assertTrue(states, "后台 sweep 应该把缓存填上")

        # 再把缓存“推”过期 31 秒：旧代码又要等 2.0 秒，新代码立刻回旧状态
        real_time = time.time

        def shifted():
            return real_time() + 31

        with mock.patch("time.time", side_effect=shifted):
            started = time.monotonic()
            resp = client.get("/api/panes/status")
            stale_elapsed = time.monotonic() - started
        self.assertEqual(resp.status_code, 200)
        self.assertLess(stale_elapsed, 1.0)
        self.assertEqual({item["id"] for item in resp.json()["panes"]},
                         {item["id"] for item in states})

    def test_sweep_backs_off_after_a_slow_sweep(self):
        """一轮 sweep 越慢，下一轮隔得越久：宁可状态旧一点，也不让 tmux 一直忙。

        缓存 ttl 是 30 秒，但一轮 sweep 结束后至少 60 秒内不再开新轮
        （慢轮按 2 倍时长再延后），这样 tmux 服务器能空下来响应用户操作。
        """
        import time

        class SlowTmux(FakeTmux):
            """抓屏一次睡 0.5 秒并计数：sweep 的开始次数就是调用次数。"""

            def __init__(self):
                super().__init__()
                self.calls = 0

            def capture_many(self, pane_ids, history=40, panes=None):
                self.calls += 1
                time.sleep(0.5)
                return super().capture_many(pane_ids, history, panes)

        tmux = SlowTmux()
        client = TestClient(create_app(
            tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:", queue_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))
        response = client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

        now = [time.time()]

        def fake_time():
            return now[0]

        with mock.patch("time.time", side_effect=fake_time):
            # 第一轮：冷缓存，立刻开扫
            client.get("/api/panes/status")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                states = client.get("/api/panes/status").json()["panes"]
                if states:
                    break
                time.sleep(0.2)
            self.assertTrue(states, "后台 sweep 应该把缓存填上")
            self.assertEqual(tmux.calls, 1)

            # 缓存过期（31 秒 > ttl 30），但上一轮刚结束 31 秒 < 间隔 60 秒：不开新轮
            now[0] += 31
            resp = client.get("/api/panes/status")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual({item["id"] for item in resp.json()["panes"]},
                             {item["id"] for item in states})
            self.assertEqual(tmux.calls, 1)

            # 再过 30 秒（共 61 秒 > 间隔 60 秒）：新一轮必须开起来
            now[0] += 30
            client.get("/api/panes/status")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if tmux.calls > 1:
                    break
                time.sleep(0.2)
            self.assertGreater(tmux.calls, 1)

    # -- "needs you": the sweep-level gate -----------------------------------
    # One menu screen, reused by the sweep tests below (claude's permission
    # confirm: cursor list + numbers, Esc to cancel).
    NEEDS_MENU = (
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again this session\n"
        "   3. No, and tell Claude what to do differently\n"
        " Esc to cancel\n"
    )
    NEEDS_MENU_MOVED = NEEDS_MENU.replace(" ❯ 1. Yes\n   2.", "   1. Yes\n ❯ 2.")

    def _wait_for_panes(self, client, predicate, timeout=10):
        """Poll /api/panes/status until predicate(states) holds (background sweep)."""
        deadline = time.monotonic() + timeout
        states = []
        while time.monotonic() < deadline:
            states = client.get("/api/panes/status").json()["panes"]
            if states and predicate(states):
                return states
            time.sleep(0.2)
        return states

    def _fresh_status_client(self):
        client = TestClient(create_app(
            self.tmux, password="unit-test-admin-password",
            session_secret="test-secret", terminal_factory=FakeTerminalFactory(),
            share_db=":memory:", queue_db=":memory:", home_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=FakeSSHRunner))
        response = client.post("/login", data={"password": "unit-test-admin-password"},
                               follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return client

    def test_an_idle_pane_with_a_stable_menu_is_not_a_native_wait(self):
        self.tmux.output = self.NEEDS_MENU
        with mock.patch("agent_status.status_for_pane", return_value="idle"):
            states = self._wait_for_panes(self._fresh_status_client(), bool)
        self.assertEqual(states[0]["status"], "idle")
        self.assertIsNone(states[0]["prompt"])
        self.assertFalse(states[0]["needs_input"])

    def test_needs_input_never_fires_while_the_agent_is_just_executing(self):
        """agent 只是在执行（没有决策点）：菜单一直在屏上也绝不亮 —— stale prompt 的标准形态。"""
        client = self._fresh_status_client()
        self.tmux.output = self.NEEDS_MENU
        now = [time.time()]

        def fake_time():
            return now[0]

        with mock.patch("agent_status.status_for_pane", return_value="busy"), \
                mock.patch("agent_status.decision_point_for_pane", return_value=False), \
                mock.patch("time.time", side_effect=fake_time):
            self._wait_for_panes(client, lambda rows: len(rows) == 1)
            now[0] += 61
            states = self._wait_for_panes(client, lambda rows: len(rows) == 1)
            self.assertFalse(states[0]["needs_input"])
            self.assertIsNone(states[0]["prompt"])
            now[0] += 61
            states = self._wait_for_panes(client, lambda rows: len(rows) == 1)
            self.assertFalse(states[0]["needs_input"], "执行时攒再多轮也没用")

    def test_an_outstanding_tool_call_does_not_confirm_a_menu(self):
        self.tmux.output = self.NEEDS_MENU
        with mock.patch("agent_status.status_for_pane", return_value="busy"), \
                mock.patch("agent_status.decision_point_for_pane", return_value=True):
            states = self._wait_for_panes(self._fresh_status_client(), bool)
        self.assertEqual(states[0]["status"], "busy")
        self.assertIsNone(states[0]["prompt"])

    def test_needs_input_never_fires_without_a_session_file_verdict(self):
        """没有 verdict 的 provider（hermes / shell / 未知）：fail closed。"""
        client = self._fresh_status_client()
        self.tmux.output = self.NEEDS_MENU
        now = [time.time()]

        def fake_time():
            return now[0]

        with mock.patch("agent_status.status_for_pane", return_value="unknown"), \
                mock.patch("agent_status.decision_point_for_pane", return_value=False), \
                mock.patch("time.time", side_effect=fake_time):
            self._wait_for_panes(client, lambda rows: len(rows) == 1)
            now[0] += 61
            states = self._wait_for_panes(client, lambda rows: len(rows) == 1)
            self.assertFalse(states[0]["needs_input"])
            self.assertIsNone(states[0]["prompt"])


    def test_a_remote_pane_uses_the_same_gate(self):
        """远端 pane：turn verdict 和抓屏同一包回来（capture_and_status_many），
        门的三个腿和本机一模一样。"""
        menu = self.NEEDS_MENU

        class MenuRunner(FakeSSHRunner):
            def capture_and_status_many(self, pane_ids, history=40, status_ids=None, panes=None):
                # verdict 是 busy（turn 在跑），decision 是 True（tool 调用 outstanding）：
                # 这正是远端权限等待在网线那头的样子，两个值必须同一包回来。
                return ({pane_id: menu for pane_id in pane_ids},
                        {pane_id: "busy" for pane_id in (status_ids or ())},
                        {pane_id: True for pane_id in (status_ids or ())})

            def status_many(self, pane_ids):
                # 没有这个方法 sweep 根本不会问 verdict（status_ids 留空），
                # 远端 pane 就只能靠抓屏猜状态 —— 那正是本 PR 要修的毛病。
                return {pane_id: "idle" for pane_id in pane_ids}

        client = TestClient(create_app(
            self.tmux, password="unit-test-admin-password",
            session_secret="test-secret", terminal_factory=FakeTerminalFactory(),
            share_db=":memory:", queue_db=":memory:", home_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=MenuRunner))
        response = client.post("/login", data={"password": "unit-test-admin-password"},
                               follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        })
        now = [time.time()]

        def fake_time():
            return now[0]

        with mock.patch("time.time", side_effect=fake_time):
            states = self._wait_for_panes(client, lambda rows: len(rows) == 2)
            remote = next(item for item in states if "~" in item["id"])
            self.assertEqual(remote["status"], "busy")
            self.assertFalse(remote["needs_input"], "第一轮：只记不亮")
            self.assertIsNone(remote["prompt"])
            self.assertEqual(remote["status_source"], "session")

    def test_the_needs_you_card_is_wired(self):
        """卡片脚本、bundle、控制台页、侧栏树：缺一环前面都白做。"""
        web = Path(__file__).resolve().parents[1] / "web"
        self.assertNotIn('id="needs-you-card"', (web / "index.html").read_text())
        self.assertIn("TmuxPrompt?.showStatus", (web / "assets/console-app.js").read_text())
        shell = (web / "assets" / "shell.js").read_text()
        for marker in ("tneed", "needs-you", "state.needs"):
            self.assertIn(marker, shell, marker)
        self.assertIn(".tneed{", (web / "assets" / "shell.css").read_text())

    def test_every_pane_gets_a_stable_short_number(self):
        """多服务器场景：每个窗口一个 #N，和主机/pane id 无关，编号不回收。"""
        # 独立的 home.db：编号是全局的，不能和别的用例共用一张表
        self.client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                            session_secret="test-secret",
                                            terminal_factory=self.terminals,
                                            share_db=":memory:", queue_db=":memory:", home_db=":memory:",
                                            remote_store=self.remote_store,
                                            remote_runner_factory=FakeSSHRunner))
        self.login()
        first = self.client.get("/api/panes").json()["panes"]
        numbers = {pane["id"]: pane["number"] for pane in first}
        self.assertTrue(numbers)
        self.assertEqual(len(set(numbers.values())), len(numbers), "编号必须唯一")
        # 同一个 pane 再来一次，号码不变
        again = self.client.get("/api/panes").json()["panes"]
        self.assertEqual({pane["id"]: pane["number"] for pane in again}, numbers)
        # 新 pane 排在新号后面，旧号不动
        self.tmux.panes.append({**self.tmux.panes[0], "id": "%99", "target": "sess:0.99"})
        grown = {pane["id"]: pane["number"] for pane in self.client.get("/api/panes").json()["panes"]}
        self.assertGreater(grown["%99"], max(numbers.values()))     # 新号排在后面，旧号不动
        for pane_id, number in numbers.items():
            self.assertEqual(grown[pane_id], number)

    def test_the_number_is_shown_in_every_title_bar(self):
        """#N 不只挂在侧栏：控制台顶栏、/grid 每格标题、/preview 标题都要有。"""
        self.login()
        page = self.console_surface()
        self.assertIn('class="session-num" id="selected-number"', page)
        self.assertIn("$('selected-number')", page)
        # 编号统一走 paneLabel()：联邦节点显示 ddr#12，本机仍是 #12
        self.assertIn("function paneLabel(pane)", page)
        self.assertIn("num.textContent=p.number?paneLabel(p):''", page)
        self.assertIn(".session-num{flex:none", page)

        grid = self.client.get("/grid").text
        self.assertIn("const prefix=meta&&meta.number?`#${meta.number} · `:''", grid)

        preview = self.client.get("/preview?pane=%2528").text
        self.assertIn("function paintTitle(){", preview)
        self.assertIn("state.paneNumber", preview)
        self.assertIn("`#${number} · ${name}`", preview)

        # 终端页标题栏 + PWA 窗口标题
        term = self.client.get("/term?pane=%2528").text
        self.assertIn("function paintTitle(number){", term)
        self.assertIn("fetch('/api/panes'", term)
        self.assertIn("`#${number} · `:'')+`终端 · ${pane}`", term)

        # 控制台的浏览器/PWA 标题
        # 标题优先用「最近 / 收藏」里的名字（钉子区的 #6），没有才用 pane 标题
        self.assertIn("const shownTitle=p.custom_name||customName||p.title||p.target;", page)
        self.assertIn("document.title=p.number?`${paneLabel(p)} · ${shownTitle} · Tmux Chat`", page)

        # 多窗口：只有一个窗口或最大化时窗口标题带 #N，删空回到「多窗口」
        grid = self.client.get("/grid").text
        self.assertIn("function syncDocTitle(){", grid)
        self.assertIn("document.querySelectorAll('#grid > .cell')", grid)
        self.assertIn("document.title=`${titleFor(pane)} · TmuxWeb`", grid)

    def test_the_sidebar_and_relay_address_a_pane_by_its_number(self):
        """#N 能当 relay 目标，也能直接 ?pane=#N 打开；侧栏行显示同一个号。"""
        self.login()
        panes = self.client.get("/api/panes").json()["panes"]
        local = next(pane for pane in panes if pane["id"] == "%4")
        handle = f"#{local['number']}"
        self.assertEqual(self.client.get(f"/api/panes/@{local['number']}/capture").status_code, 200)
        self.tmux.auto_reply = "编号回答"
        response = self.client.post("/api/relay/ask", json={
            "target": handle, "question": "hi", "timeout": 5, "source": "unit-test"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reply"], "编号回答")
        # 不存在的号：404 且带解释，不会落到别人的会话
        missing = self.client.post("/api/relay/ask", json={
            "target": "#9999", "question": "hi", "timeout": 5})
        self.assertEqual(missing.status_code, 404)
        self.assertIn("9999", missing.json()["detail"])

        # read_agent / 任意 /api/panes/<ref> 也认 #N（MCP 工具不是只走 relay_target）
        capture = self.client.get(f"/api/panes/{handle.replace('#', '%23')}/capture?history=5")
        self.assertEqual(capture.status_code, 200, capture.text)
        self.assertEqual(capture.json()["pane"], handle)
        transcript = self.client.get(f"/api/panes/{handle}/transcript?limit=2")
        self.assertEqual(transcript.status_code, 200, transcript.text)
        self.assertEqual(self.client.get("/api/panes/%239999/capture").status_code, 404)

        page = self.console_surface()
        self.assertIn('class="pane-num"', page)
        self.assertIn("/^#?\\d{1,5}$/.test(wanted.trim())", page)
        self.assertIn("state.panes.find(p=>p.number===number)", page)
        self.assertIn("${paneLabel(p)}", page, "侧栏编号也走 paneLabel（联邦显示 ddr#12）")
        recents = self.client.get("/assets/recents.js").text
        self.assertIn("entry.number ? '#' + entry.number", recents)
        # 「最近」的行也带同一个编号
        recent = self.client.get("/api/home/recent").json()["recent"]
        self.assertTrue(all(entry.get("number") for entry in recent) or not recent)

    def test_relay_ask_returns_the_marked_reply(self):
        self.tmux.auto_reply = "本地回答"
        self.login()
        response = self.client.post("/api/relay/ask", json={
            "target": "%4", "question": "1+1 等于几？", "timeout": 10, "source": "unit-test",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "本地回答")
        self.assertEqual(response.json()["provider"], "codex")
        self.assertEqual(response.json()["source"], "unit-test")
        self.assertIn("unit-test", self.tmux.sent[-1][1])
        self.assertIn("TMUXWEB_RELAY", self.tmux.sent[-1][1])
        events = self.client.get("/api/relay/log").json()["events"]
        self.assertEqual(events[0]["status"], "ok")

    def test_relay_ask_reaches_a_remote_pane(self):
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        host_id = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()["id"]
        FakeSSHRunner.auto_reply = "远端回答"
        try:
            response = self.client.post("/api/relay/ask", json={
                "target": f"{host_id}~%9", "question": "远端请回答", "timeout": 10,
            })
        finally:
            FakeSSHRunner.auto_reply = None
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "远端回答")
        self.assertEqual(response.json()["host"], "build-box")

    def test_relay_ask_times_out_with_the_pane_tail(self):
        self.login()
        response = self.client.post("/api/relay/ask", json={"target": "%4", "question": "hi", "timeout": 5})
        self.assertEqual(response.status_code, 504)
        self.assertIn("timed out", response.json()["detail"])
        self.assertIn("Ask Codex", response.json()["tail"])

    def test_relay_rejects_busy_panes_unless_forced(self):
        self.tmux.output = "⠋ Thinking…\n(esc to interrupt)\n"
        self.login()
        response = self.client.post("/api/relay/ask", json={"target": "%4", "question": "hi", "timeout": 5})
        self.assertEqual(response.status_code, 409)
        self.assertIn("busy", response.json()["detail"])
        self.assertEqual(self.tmux.sent, [])

    def test_relay_message_is_fire_and_forget(self):
        self.login()
        response = self.client.post("/api/relay/message", json={"target": "%4", "text": "请先跑测试"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tmux.sent[-1], ("%4", "请先跑测试", True))

    def test_script_notice_has_no_choice_authority(self):
        self.login()
        pane = self.tmux.panes[0]
        pane.update(command="bash", session="custom", pane_pid="123",
                    script_signal=f"123:{int(time.time() * 1000)}:blocked")
        self.tmux.output = "Waiting for a human"
        peer = next(item for item in self.client.get("/api/relay/peers").json()["peers"]
                    if item["id"] == "%4")
        prompt = peer["prompt"]
        self.assertTrue(peer["needs_input"])
        self.assertEqual((prompt["source"], prompt["read_only"], prompt["options"]),
                         ("script_status", True, []))
        result = self.client.post("/api/panes/%254/prompt/choose",
                                  json={"revision": prompt["revision"], "index": 0})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.tmux.keys, [])
        self.assertEqual(self.tmux.sent, [])

    def test_relay_receipt_visible_only_from_its_sender(self):
        self.login()
        self.tmux.panes.append(dict(self.tmux.panes[0], id="%5", pane_id="%5"))
        response = self.client.post("/api/relay/message",
                                    json={"target": "%4", "text": "read the report", "caller": "%5"})
        self.assertEqual(response.status_code, 200)
        receipt, = self.client.get("/api/panes/status", params={"sender": "%5"}).json()["outbound"]
        self.assertEqual((receipt["id"], receipt["child"], receipt["progress"]),
                         (response.json()["call"], "%4", "submitted"))
        self.assertEqual(self.client.get("/api/panes/status", params={"sender": "%4"}).json()["outbound"], [])

    def test_relay_context_paginates_from_confirmed_identity_and_scopes_direction(self):
        self.login()
        self.tmux.panes.append(dict(self.tmux.panes[0], id="%5", pane_id="%5"))
        call_id = self.client.post("/api/relay/message", json={
            "target": "%4", "text": "same question", "caller": "%5"}).json()["call"]
        created = self.client.get("/api/agents/calls/" + call_id).json()["created"]
        session = "same-session"
        turns = [{"id": "old", "role": "user", "timestamp": created-1, "text": "same question"},
                 {"id": "exact", "role": "user", "timestamp": created+1, "text": "same question"}]
        def transcript(pid, limit, pane):
            return {"messages": turns[-limit:], "session_id": session, "mtime": 1, "has_more": len(turns)>limit}
        self.tmux.transcript = transcript
        path = "/api/panes/%254/relay-context/" + call_id
        self.assertEqual(self.client.get(path, params={"side":"sent"}).status_code, 404)
        first = self.client.get(path, params={"side":"received"}).json()
        self.assertEqual(first["anchor"]["id"], "exact")
        turns.extend({"id": str(i), "role": "assistant", "timestamp": created+2+i, "text": "answer"}
                     for i in range(580))
        first = self.client.get(path, params={"side":"received"}).json()
        self.assertEqual(first["anchor"]["relay_receipt"]["id"], call_id)
        self.assertTrue(first["more"])
        self.assertEqual([m["id"] for m in first["messages"]], list(map(str, range(499))))
        second = self.client.get(path, params={"side":"received","offset":499}).json()
        self.assertFalse(second["more"])
        self.assertEqual(second["messages"][0]["id"], "499")
        small = self.client.get(path, params={"side":"received", "size":37, "offset":40}).json()
        self.assertEqual((small["session_id"], small["messages"][0]["id"], len(small["messages"])),
                         (session, "40", 37))
        live_path = "/api/panes/%255/transcript"
        latest = self.client.get(live_path).json()
        validation = {"sig":latest["signature"], "cached_session":session}
        self.assertTrue(self.client.get(live_path, params=validation).json()["unchanged"])
        validation["cached_session"] = "rotated-session"
        self.assertIn("messages", self.client.get(live_path, params=validation).json())
        request = {"sent_relay":call_id, "limit":800}
        placement = self.client.get("/api/panes/%255/transcript", params=request).json()["relay_sender"]
        self.assertEqual((placement["call"]["id"], placement["index"]), (call_id, 1))
        self.assertNotIn("relay_sender", self.client.get("/api/panes/%254/transcript", params=request).json())
        sent = self.client.get(path.replace("254","255",1), params={"side":"sent"}).json()
        self.assertEqual((sent["call"]["id"], sent["messages"][0]["id"]), (call_id, "exact"))
        # A repeated body and timestamp cannot stand in for the saved harness ID.
        turns[1]["id"] = "lookalike"
        self.assertNotIn("anchor", self.client.get(path, params={"side":"received"}).json())
        turns[1]["id"] = "exact"
        session = "another-session"
        missing = self.client.get(path, params={"side":"received"}).json()
        self.assertNotIn("anchor", missing)
        self.assertTrue(missing["notice"])
        self.assertEqual(missing["messages"], [])

    def test_lists_files_for_the_selected_pane(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "notes.md").write_text("hello")
            (Path(directory) / "src").mkdir()
            self.tmux.panes[0]["path"] = directory
            listing = self.client.get("/api/panes/%254/files")
        self.assertEqual(listing.status_code, 200)
        payload = listing.json()
        self.assertEqual(payload["path"], os.path.realpath(directory))
        self.assertEqual([entry["name"] for entry in payload["entries"]], ["src", "notes.md"])
        self.assertEqual(payload["pane"], "%4")

    def test_each_pane_lists_its_own_directory(self):
        """Two panes must never share a listing: the file panel is per pane/host."""
        self.login()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            Path(first, "only-in-first.txt").write_text("a")
            Path(second, "only-in-second.txt").write_text("b")
            self.tmux.panes[0]["path"] = first
            if len(self.tmux.panes) < 2:
                self.tmux.panes.append(dict(self.tmux.panes[0], pane_id="%5", id="%5"))
            self.tmux.panes[1]["path"] = second
            first_listing = self.client.get("/api/panes/%254/files").json()
            second_listing = self.client.get("/api/panes/%255/files").json()
        self.assertEqual(first_listing["path"], os.path.realpath(first))
        self.assertEqual(second_listing["path"], os.path.realpath(second))
        self.assertEqual([entry["name"] for entry in first_listing["entries"]], ["only-in-first.txt"])
        self.assertEqual([entry["name"] for entry in second_listing["entries"]], ["only-in-second.txt"])

    def test_zips_selected_files_for_download(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a.txt").write_text("alpha")
            (Path(directory) / "sub").mkdir()
            Path(directory, "sub", "b.txt").write_text("beta")
            self.tmux.panes[0]["path"] = directory
            response = self.client.post("/api/panes/%254/files/zip",
                                        json={"base": directory, "paths": [os.path.join(directory, "a.txt"),
                                                                          os.path.join(directory, "sub")]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn(".zip", response.headers["content-disposition"])
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(sorted(archive.namelist()), ["a.txt", "sub/", "sub/b.txt"])
        self.assertEqual(archive.read("a.txt"), b"alpha")
        self.assertEqual(archive.read("sub/b.txt"), b"beta")

    def test_zip_rejects_empty_and_oversized_selections(self):
        self.login()
        self.assertEqual(self.client.post("/api/panes/%254/files/zip", json={"paths": []}).status_code, 422)
        too_many = [f"/tmp/f{index}" for index in range(1001)]
        self.assertEqual(self.client.post("/api/panes/%254/files/zip",
                                          json={"paths": too_many}).status_code, 422)

    def test_proxy_headers_are_only_trusted_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Path(root, "notes.txt").write_text("x")
            headers = {"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-Proto": "https"}
            # trusted proxy: cookie is Secure and the limit keys on the real client
            with patch.dict("os.environ", {"TMUX_WEB_TRUST_PROXY": "1", "TMUX_WEB_SECURE_COOKIES": "auto"}):
                client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                               session_secret="test-secret", terminal_factory=self.terminals,
                                               share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                               remote_runner_factory=FakeSSHRunner))
                response = client.post("/login", data={"password": "unit-test-admin-password"}, headers=headers,
                                       follow_redirects=False)
                self.assertIn("secure", response.headers["set-cookie"].lower())
            # untrusted proxy: the forwarded protocol is ignored
            with patch.dict("os.environ", {"TMUX_WEB_TRUST_PROXY": "", "TMUX_WEB_SECURE_COOKIES": "auto"}):
                client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                               session_secret="test-secret", terminal_factory=self.terminals,
                                               share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                               remote_runner_factory=FakeSSHRunner))
                response = client.post("/login", data={"password": "unit-test-admin-password"}, headers=headers,
                                       follow_redirects=False)
                self.assertNotIn("secure", response.headers["set-cookie"].lower())

    def test_allowed_hosts_can_be_pinned(self):
        with patch.dict("os.environ", {"TMUX_WEB_ALLOWED_HOSTS": "chat.example.com"}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            strays = client.get("/login", headers={"Host": "evil.example.net"})
            allowed = client.get("/login", headers={"Host": "chat.example.com"})
            local = client.get("/login", headers={"Host": "127.0.0.1:8787"})
        self.assertEqual(strays.status_code, 421)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(local.status_code, 200)

    def test_shell_panes_can_be_shared_as_a_screen(self):
        self.login()
        self.tmux.panes[0]["command"] = "zsh"          # no transcript provider
        created = self.client.post("/api/shares", json={"pane_id": "%4", "slug": "shell-share",
                                                        "expires_in": 3600})
        self.assertEqual(created.status_code, 201)
        data = self.client.get("/share/shell-share/api/transcript").json()
        self.assertEqual(data["mode"], "screen")
        self.assertIsNone(data["session_id"])
        self.assertIn("Ask Codex", data["content"])
        self.assertEqual(data["messages"], [])
        # revoking still works and a missing pane no longer breaks the share
        shares = self.client.get("/api/shares").json()["shares"]
        share_id = next(item["id"] for item in shares if item["slug"] == "shell-share")
        self.assertEqual(self.client.delete(f"/api/shares/{share_id}").status_code, 200)

    def test_an_expired_snapshot_answers_at_once_and_refreshes_behind_it(self):
        """The sidebar must never wait on the slowest host: ping first, panes later."""
        calls = {"count": 0}
        tmux = FakeTmux()
        original = tmux.list_panes

        def counting(*args, **kwargs):
            calls["count"] += 1
            return original(*args, **kwargs)

        tmux.list_panes = counting
        with mock.patch.dict(os.environ, {"TMUX_WEB_HOSTS_TTL": "0"}):
            client = TestClient(create_app(
                tmux, password="unit-test-admin-password", session_secret="test-secret",
                terminal_factory=self.terminals, share_db=":memory:", queue_db=":memory:",
                remote_store=self.remote_store, remote_runner_factory=FakeSSHRunner,
            ))
        client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)

        first = client.get("/api/hosts").json()["hosts"]
        self.assertTrue(first)
        after_first = calls["count"]

        # TTL 0 => always expired: the second answer is the cached one, served
        # without touching tmux, and a sweep is kicked off in the background.
        second = client.get("/api/hosts").json()["hosts"]
        self.assertEqual([h["id"] for h in second], [h["id"] for h in first])
        self.assertEqual(second[0]["panes"], first[0]["panes"])

        for _ in range(40):
            if calls["count"] > after_first:
                break
            time.sleep(0.05)
        self.assertGreater(calls["count"], after_first,
                           "the background sweep never ran")

    def test_concurrent_cold_sidebar_reads_share_one_probe(self):
        import httpx
        calls = []
        started, release = threading.Event(), threading.Event()
        original = self.tmux.list_panes
        def slow():
            calls.append(1)
            started.set()
            release.wait(4)
            return original()

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app),
                                         base_url="http://localhost") as client:
                await client.post("/login", data={"password":"unit-test-admin-password"})
                readers = [asyncio.create_task(client.get("/api/hosts")) for _ in range(12)]
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await asyncio.sleep(0.05)
                release.set()
                for response in await asyncio.gather(*readers):
                    self.assertEqual(response.json()["hosts"][0]["status"], "online")
        with mock.patch.object(self.tmux, "list_panes", side_effect=slow):
            asyncio.run(exercise())
        self.assertEqual(len(calls), 1)

    def test_hosts_list_is_snapshotted_between_polls(self):
        calls = {"count": 0}
        original = self.tmux.list_panes

        def counting(*args, **kwargs):
            calls["count"] += 1
            return original(*args, **kwargs)

        self.tmux.list_panes = counting
        self.login()
        self.client.get("/api/hosts")
        first = calls["count"]
        self.assertGreaterEqual(first, 1)

        # a second poll inside the ttl must not touch tmux/SSH again
        self.client.get("/api/hosts")
        self.assertEqual(calls["count"], first)

        # the refresh button forces a real sweep
        self.client.get("/api/hosts", params={"refresh": 1})
        self.assertGreater(calls["count"], first)

    def test_mcp_endpoint_speaks_streamable_http(self):
        self.login()
        init = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init.status_code, 200)
        self.assertEqual(init.json()["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init.json()["result"]["serverInfo"], {"name":"tmuxweb-relay", "version":tmux_web.RELEASE_VERSION})

        listing = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
        self.assertEqual({tool["name"] for tool in listing["result"]["tools"]},
                         {"list_projects", "create_project", "join_project", "leave_project", "remove_project_member",
                          "list_agents", "ask_agent", "send_agent", "request_file_review", "relay_tasks", "run_command",
                          "create_session", "kill_session", "send_file", "read_agent",
                          "publish_home_card", "list_home", "remove_home_card",
                          "list_remote_hosts", "add_remote_host", "remove_remote_host"})

        agents = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                "params": {"name": "list_agents", "arguments": {}}}).json()
        payload = json.loads(agents["result"]["content"][0]["text"])
        self.assertEqual(payload["agents"][0]["target"], "%4")
        self.assertEqual(payload["agents"][0]["provider"], "codex")

        read = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                              "params": {"name": "read_agent",
                                                         "arguments": {"target": "%4", "lines": 20}}}).json()
        self.assertIn("Ask Codex", json.loads(read["result"]["content"][0]["text"])["content"])

        # notifications have no body, unknown methods are refused, GET is not allowed
        note = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(note.status_code, 202)
        unknown = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(self.client.get("/mcp").status_code, 405)

    def test_mcp_can_run_without_auth_on_a_trusted_network(self):
        with patch.dict("os.environ", {"TMUX_WEB_MCP_AUTH": "off"}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            open_mcp = client.post("/mcp", json=body)
            # the rest of the API keeps its token/cookie check
            still_protected = client.get("/api/hosts")
        self.assertEqual(open_mcp.status_code, 200)
        self.assertEqual(len(open_mcp.json()["result"]["tools"]), 21)
        self.assertEqual(still_protected.status_code, 401)

    def test_mcp_endpoint_requires_the_api_token(self):
        anonymous = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                         session_secret="test-secret", terminal_factory=self.terminals,
                                         share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                         remote_runner_factory=FakeSSHRunner))
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        self.assertEqual(anonymous.post("/mcp", json=body).status_code, 401)
        token = "unit-test-api-token"
        with patch.dict("os.environ", {"TMUX_WEB_API_TOKEN": token}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            allowed = client.post("/mcp", json=body, headers={"Authorization": "Bearer " + token})
        self.assertEqual(allowed.status_code, 200)

    def test_mcp_tool_errors_are_reported_as_tool_results(self):
        self.login()
        missing = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": "read_agent", "arguments": {}}}).json()
        self.assertTrue(missing["result"]["isError"])
        unknown = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": "nope", "arguments": {}}}).json()
        self.assertIn("unknown tool", unknown["result"]["content"][0]["text"])

    def test_creates_a_local_session(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            response = self.client.post("/api/sessions",
                                        json={"host": "localhost", "name": "agent-test", "path": directory})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["session"]["session"], "agent-test")
        self.assertEqual(body["host"], "localhost")
        self.assertEqual([pane["id"] for pane in body["panes"]], ["%12"])
        self.assertEqual(self.tmux.created, [("agent-test", directory)])

    def test_creates_a_missing_directory_before_starting_the_session(self):
        self.login()
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "proj" / "nested"
            response = self.client.post("/api/sessions",
                                        json={"name": "fresh", "path": str(target)})
            self.assertEqual(response.status_code, 201)
            self.assertTrue(target.is_dir(), "创建会话时应顺手把目录建出来")
            self.assertEqual(self.tmux.created, [("fresh", str(target))])
            self.assertTrue(response.json()["session"]["created_dir"])

    def test_create_dir_false_keeps_the_missing_directory_error(self):
        self.login()
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "absent"
            response = self.client.post("/api/sessions",
                                        json={"name": "keep", "path": str(target), "create_dir": False})
        self.assertEqual(response.status_code, 422)
        self.assertIn("no such directory", response.json()["detail"])
        self.assertFalse(target.exists())

    def test_starts_the_session_with_an_agent_and_its_arguments(self):
        self.login()
        installed = [{"name": "codex", "label": "Codex", "path": "/usr/local/bin/codex"}]
        with mock.patch.object(tmux_web.agent_catalog, "probe_local", return_value=installed):
            with tempfile.TemporaryDirectory() as directory:
                response = self.client.post("/api/sessions", json={
                    "name": "agent-run", "path": directory,
                    "agent": "codex", "agent_args": '--model o3 -c "a b"'})
        self.assertEqual(response.status_code, 201)
        # 用探测到的绝对路径：tmux pane 的 PATH 和 ssh 探测时不一定一样
        self.assertEqual(self.tmux.created_commands[0],
                         ("agent-run", ["/usr/local/bin/codex", "--model", "o3", "-c", "a b"]))
        self.assertEqual(response.json()["session"]["command"], "/usr/local/bin/codex --model o3 -c 'a b'")
        # agent 退出后会话不被带走：ctyun 上 claude 不在 PATH，127 直接把会话关了
        shell = self.tmux.created_shell[0]
        self.assertIn('exec "${SHELL:-bash}"', shell)
        self.assertIn("已退出", shell)

    def test_kimi_chat_launch_preserves_the_terminal_default(self):
        self.login()
        installed = [{"name":"kimi","label":"Kimi","path":"/fixture/kimi"}]
        with mock.patch.object(tmux_web.agent_catalog,"probe_local",return_value=installed):
            for name, mode in (("plain",False),("structured",True)):
                response=self.client.post("/api/sessions",json={"name":name,"agent":"kimi","kimi_chat":mode})
                self.assertEqual(response.status_code,201,response.text)
                cmd=self.tmux.created_commands[-1][1]
                self.assertEqual(cmd[-1],"/fixture/kimi")
                self.assertEqual(any(part.endswith("kimi_acp.py") for part in cmd),mode)
            refused=self.client.post("/api/sessions",json={"name":"bad","agent":"kimi",
                                                           "agent_args":"--continue","kimi_chat":True})
            self.assertEqual(refused.status_code,422)

    def test_keep_open_can_be_turned_off(self):
        self.login()
        installed = [{"name": "codex", "label": "Codex", "path": "/usr/bin/codex"}]
        with mock.patch.object(tmux_web.agent_catalog, "probe_local", return_value=installed):
            response = self.client.post("/api/sessions", json={
                "name": "no-keep", "agent": "codex", "keep_open": False})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.tmux.created_shell[0], "/usr/bin/codex")

    def test_agent_must_be_known_and_installed(self):
        self.login()
        with mock.patch.object(tmux_web.agent_catalog, "probe_local", return_value=[]):
            for name, agent in (("x1", "nope"), ("x2", "claude")):
                response = self.client.post("/api/sessions", json={"name": name, "agent": agent})
                self.assertEqual(response.status_code, 422)
                self.assertIn("not installed", response.json()["detail"])

        installed = [{"name": "claude", "label": "Claude", "path": "/usr/bin/claude"}]
        with mock.patch.object(tmux_web.agent_catalog, "probe_local", return_value=installed):
            self.client.get("/api/hosts/localhost/agents", params={"refresh": True})
        response = self.client.post("/api/sessions", json={
            "name": "x3", "agent": "claude", "agent_args": '--x "unclosed',
        })
        self.assertEqual(response.status_code, 422)
        self.assertIn("agent arguments", response.json()["detail"])

    def test_lists_the_agents_a_host_can_run(self):
        self.login()
        installed = [{"name": "hermes", "label": "Hermes", "path": "/home/u/.local/bin/hermes"}]
        with mock.patch.object(tmux_web.agent_catalog, "probe_local", return_value=installed) as probe:
            first = self.client.get("/api/hosts/localhost/agents")
            second = self.client.get("/api/hosts/localhost/agents")
            self.client.get("/api/hosts/localhost/agents", params={"refresh": 1})
        self.assertEqual(first.status_code, 200)
        self.assertEqual([item["name"] for item in first.json()["agents"]], ["hermes"])
        self.assertEqual(second.json()["agents"], first.json()["agents"])
        self.assertEqual(probe.call_count, 2, "第二次应该走缓存，refresh=1 才重新探测")

    def test_lists_agents_on_a_remote_host_over_ssh(self):
        host_key = ("10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l"
                    "+Kg3QGMCbo2")
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        host = self.client.post("/api/remote-hosts", json={
            "name": "agent-box", "hostname": "10.0.0.9", "port": 22, "username": "ubuntu",
            "auth_type": "agent", "credential": "", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        FakeSSHRunner.agents = [{"name": "kimi", "label": "Kimi Code", "path": "/home/u/.kimi-code/bin/kimi"}]
        try:
            response = self.client.get("/api/hosts/%s/agents" % host["id"])
        finally:
            FakeSSHRunner.agents = []
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["name"] for item in response.json()["agents"]], ["kimi"])

        response = self.client.post("/api/sessions", json={
            "host": host["id"], "name": "remote-agent", "path": "/srv/new dir",
            "agent": "kimi", "agent_args": "--yolo"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(FakeSSHRunner.created_command,
                         ["/home/u/.kimi-code/bin/kimi", "--yolo"])
        self.assertTrue(FakeSSHRunner.created_create_dir)
        self.assertTrue(FakeSSHRunner.created_keep_open)

    def test_session_creation_validates_name_path_and_duplicates(self):
        self.login()
        self.assertEqual(self.client.post("/api/sessions", json={"name": "bad:name"}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions", json={"name": "x y"}).status_code, 422)
        # 缺失的目录现在会自动创建（c211fad），不再 422
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "fresh", "project")
            created = self.client.post("/api/sessions",
                                       json={"name": "ok", "path": missing})
            self.assertEqual(created.status_code, 201, created.text)
            self.assertTrue(created.json()["session"]["created_dir"])
            self.assertTrue(os.path.isdir(missing))
        self.assertEqual(self.client.post("/api/sessions", json={"name": "codex"}).status_code, 409)
        self.assertEqual(self.client.post("/api/sessions", json={"host": "deadbeefdeadbeef",
                                                                 "name": "ok"}).status_code, 404)

    def test_hook_notice_reaches_chat_without_a_menu(self):
        self.tmux.needs_input_many = lambda panes, timeout: {
            "%4": {"notices": [{"title": "需要回答问题", "detail": "Which branch?", "id": ["s", "q"]}]}}
        self.tmux.output = "still working"
        client = self._fresh_status_client()
        states = self._wait_for_panes(client, lambda rows: any(p.get("needs_input") for p in rows))
        prompt = next(p["prompt"] for p in states if p["id"] == "%4")
        self.assertTrue(prompt["read_only"])
        self.assertEqual(prompt["kind"], "notice")
        self.assertEqual(prompt["options"], [{"label": "需要回答问题", "description": "Which branch?"}])

    def test_codex_models_follow_the_session_catalog(self):
        self.login()
        self.tmux.panes[0].update(provider="codex", command="codex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.tmux.transcript = lambda pane_id, limit=1, pane=None: {
                "provider": "codex", "messages": [], "model": "previous",
                "path": str(root / "sessions/rollout.jsonl")}
            url = "/api/panes/%254/models"
            empty = self.client.get(url).json()
            self.assertEqual(empty["presets"], [])
            self.assertTrue(empty["picker_only"])
            (root / "models_cache.json").write_text('{"models":[{"slug":"visible","visibility":"list"}]}')
            body = self.client.get(url).json()
            self.assertEqual(body["presets"], ["visible"])
            self.assertEqual(body["current"], "previous")
            self.assertTrue(body["native_picker"])

    def test_lists_the_models_a_pane_can_switch_to(self):
        self.login()
        self.tmux.panes[0].update({"command": "claude", "provider": "claude"})
        self.tmux.transcript = lambda pane_id, limit=1, pane=None: {
            "provider": "claude", "messages": [{"id": "1"}], "model": "claude-opus-5-5"}
        response = self.client.get("/api/panes/%254/models")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "claude")
        self.assertEqual(body["current"], "claude-opus-5-5")
        self.assertIn("opus", body["presets"])
        self.assertEqual(body["provider_label"], "Anthropic")
        self.assertFalse(body["picker_only"])

    def test_hermes_groups_and_provider_switch(self):
        self.login()
        self.tmux.panes[0].update({"command": "hermes", "provider": "hermes"})
        self.tmux.transcript = lambda pane_id, limit=1, pane=None: {
            "provider": "hermes", "messages": [{"id": "1"}], "model": "deepseek-flash",
            "model_provider": "axera"}
        groups = [{"provider": "axera", "models": ["deepseek-flash", "glm-5.3"]},
                  {"provider": "ctyun", "models": ["gpt-5.6-sol"]}]
        with mock.patch.object(tmux_web.agent_models, "hermes_groups", return_value=groups):
            body = self.client.get("/api/panes/%254/models").json()
        self.assertEqual([group["provider"] for group in body["groups"]], ["axera", "ctyun"])
        self.assertEqual(body["current_provider"], "axera")
        sent = []
        self.tmux.send = lambda pane_id, text, submit=True, pane=None: sent.append(text)
        response = self.client.post("/api/panes/%254/model", json={"model": "gpt-5.6-sol", "provider": "ctyun"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["command"], "/model gpt-5.6-sol --provider ctyun")
        self.assertEqual(sent, ["/model gpt-5.6-sol --provider ctyun"])

    def test_switching_model_types_the_slash_command(self):
        self.login()
        self.tmux.panes[0].update({"command": "claude", "provider": "claude"})
        sent = []
        self.tmux.send = lambda pane_id, text, submit=True, pane=None: sent.append((pane_id, text, submit))
        response = self.client.post("/api/panes/%254/model", json={"model": "opus"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["command"], "/model opus")
        self.assertEqual(sent[0][:3], ("%4", "/model opus", True))

    def test_kimi_only_opens_its_picker(self):
        self.login()
        self.tmux.panes[0].update({"command": "kimi-code", "provider": "kimi"})
        sent = []
        self.tmux.send = lambda pane_id, text, submit=True, pane=None: sent.append(text)
        response = self.client.post("/api/panes/%254/model", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["command"], "/model")
        self.assertEqual(sent, ["/model"])

    def test_an_unchanged_transcript_poll_returns_a_stub(self):
        """2 秒一次轮询，不能每次都把 300 条消息重发一遍（85KB → 127B）。"""
        self.login()
        messages = [{"id": "1", "role": "user", "text": "hi", "timestamp": 1},
                    {"id": "2", "role": "assistant", "text": "hello", "timestamp": 2}]
        self.tmux.transcript = lambda pane_id, limit=200, pane=None: {
            "provider": "hermes", "session_id": "s1", "messages": messages, "mtime": 111}
        first = self.client.get("/api/panes/%254/transcript?limit=300").json()
        self.assertEqual(first["signature"], "111|2|2")
        again = self.client.get(f"/api/panes/%254/transcript?limit=300&sig={first['signature']}").json()
        self.assertEqual(again["unchanged"], True)
        self.assertNotIn("messages", again)
        changed = self.client.get("/api/panes/%254/transcript?limit=300&sig=999|9|z").json()
        self.assertNotIn("unchanged", changed)
        self.assertEqual(len(changed["messages"]), 2)

    def test_tail_returns_plain_text_and_a_digest(self):
        self.login()
        self.tmux.capture = lambda pane_id, history=500, pane=None: "a\n\n  b  \nc\n"
        # 最少 3 行：只给 1-2 行时看不出在干什么
        body = self.client.get("/api/panes/%254/tail?lines=3").json()
        self.assertEqual(body["text"], "a\n  b\nc")
        self.assertEqual(len(body["digest"]), 40)

    def test_tail_menu_requires_a_cursor_and_current_navigation_footer(self):
        self.login()
        self.tmux.panes[0].update(command="claude", provider="claude")
        menu = "Select a model\n❯ 1. Alpha\n  2. Beta\nEnter select · Esc cancel"
        path = "/api/panes/%254/tail?lines=12"
        self.tmux.capture = mock.Mock(return_value=menu)
        preview = self.client.get(path).json()["screen_menu"]
        self.assertEqual(preview["labels"], ["Alpha", "Beta"])
        self.assertEqual(preview["selected"], 0)
        self.tmux.capture.return_value = ("╭────╮\n│ ❯ [ ] Linux │\n│   [x] macOS │\n"
                                          "│ ↑↓ navigate · Space toggle · Enter submit │\n╰────╯")
        self.assertEqual(self.client.get(path).json()["screen_menu"]["labels"], ["Linux", "macOS"])
        for text in (menu.replace("❯", " "), menu + "\nFinished", "1. Alpha\n2. Beta"):
            self.tmux.capture.return_value = text
            self.assertNotIn("screen_menu", self.client.get(path).json())
        self.assertFalse(self.tmux.keys)

    def test_tail_navigation_checks_the_screen_and_never_confirms(self):
        path = "/api/panes/%254/tail"
        move = path + "/navigate"
        self.tmux.panes[0].update(command="claude", provider="claude", pane_pid="4242")
        menu = "Select a model\n❯ 1. Alpha\n  2. Beta\nEnter select · Esc cancel"
        self.tmux.capture_ansi = mock.Mock(return_value=menu)
        self.assertEqual(self.client.post(move, json={"key": "Down", "snapshot": "0"*64}).status_code, 401)
        self.login()
        token = self.client.get(path + "?lines=12&color=1").json()["screen_menu"]["snapshot"]
        def send(key):
            return self.client.post(move, json={"key": key, "snapshot": token}).status_code
        self.assertEqual(send("Enter"), 422)
        self.tmux.panes[0]["pane_pid"] = "4243"
        self.assertEqual(send("Up"), 409)
        self.tmux.panes[0]["pane_pid"] = "4242"
        self.tmux.capture_ansi.return_value = menu + "\nDone"
        self.assertEqual(send("Down"), 409)
        self.assertFalse(self.tmux.keys)
        self.tmux.capture_ansi.return_value = menu
        self.assertEqual(send("Down"), 200)
        self.assertTrue(self.tmux.capture_ansi.call_args.kwargs["visible_only"])
        self.assertEqual(send("Down"), 409)
        self.assertEqual(self.tmux.keys, [("%4", "Down")])

    def test_color_tail_keeps_inherited_sgr_and_ignores_non_display_controls(self):
        self.login()
        def capture(color):
            return f"\x1b[{color}mold\n\x1b[2J\nA\nB\n\x1b]8;;https://example.test\x1b\\<b>C</b>\x1b]8;;\x07"
        self.tmux.capture_ansi = mock.Mock(side_effect=[capture(31), capture(32)])
        url = "/api/panes/%254/tail?lines=3&color=1"
        first, second = (self.client.get(url).json() for _ in range(2))
        self.assertEqual(first["text"], "A\nB\n<b>C</b>")
        self.assertEqual(first["ansi"], "\x1b[31mA\nB\n<b>C</b>")
        self.assertNotEqual(first["digest"], second["digest"])
        dense = tmux_web.terminal_fragment("\x1b[31mX" * 200, 12)
        self.assertEqual(dense["text"], "X" * 200)
        self.assertNotIn("ansi", dense)

    def test_simultaneous_tail_viewers_share_capture_and_retry_after_failure(self):
        import asyncio
        import httpx

        self.login()
        def capture(*args, **kwargs):
            time.sleep(0.08)
            raise RuntimeError("fixture offline")
        self.tmux.capture = mock.Mock(side_effect=capture)
        path = "/api/panes/%254/tail?lines=12"

        async def read():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app),
                                         base_url="http://testserver", cookies=self.client.cookies) as client:
                responses = await asyncio.gather(*(client.get(path) for _ in range(8)))
                self.assertEqual([r.status_code for r in responses], [503] * 8)
                self.assertEqual(self.tmux.capture.call_count, 1)
                self.tmux.capture.side_effect = None
                self.tmux.capture.return_value = "X" * 17000
                response = await client.get(path)
                self.assertEqual(response.json()["text"], "X" * 16384)
                self.assertEqual(self.tmux.capture.call_count, 2)

        asyncio.run(read())

    def test_chunked_upload_appends_then_renames(self):
        """4MB 一块：中途失败只丢一块，最后一块落盘才改名，别人看不到半个文件。"""
        import hashlib
        import tempfile

        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            chunks = [b"a" * 1000, b"b" * 1000, b"c" * 500]
            total = sum(len(chunk) for chunk in chunks)
            offset = 0
            for index, chunk in enumerate(chunks):
                response = self.client.put(
                    f"/api/panes/%254/files/upload?name=big.bin&dir={directory}"
                    f"&offset={offset}&total={total}&token=abc", content=chunk)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                offset += len(chunk)
                if index < len(chunks) - 1:
                    self.assertEqual(body["status"], "partial")
                    self.assertEqual(body["offset"], offset)
                    # 还没写完时目标文件不能出现
                    self.assertFalse((Path(directory) / "big.bin").exists())
            self.assertEqual(body["status"], "uploaded")
            payload = (Path(directory) / "big.bin").read_bytes()
            self.assertEqual(payload, b"".join(chunks))
            self.assertEqual(hashlib.sha256(payload).hexdigest(),
                             hashlib.sha256(b"".join(chunks)).hexdigest())
            self.assertFalse(list(Path(directory).glob("*.part")))

    def test_chunk_with_a_wrong_offset_reports_where_the_file_is(self):
        import tempfile

        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            self.client.put(f"/api/panes/%254/files/upload?name=x.bin&dir={directory}"
                            f"&offset=0&total=2000&token=t1", content=b"z" * 1000)
            response = self.client.put(f"/api/panes/%254/files/upload?name=x.bin&dir={directory}"
                                       f"&offset=1500&total=2000&token=t1", content=b"z" * 500)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["offset"], 1000)
            self.assertEqual(response.headers.get("x-upload-offset"), "1000")

    def test_the_upload_helper_is_served(self):
        self.login()
        response = self.client.get("/assets/uploads.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("TmuxUpload", response.text)
        # 首块 1MB 起步（30MB 文件按 total/8 是 3.75MB，40KB/s 下 ~94s 就撞
        # Cloudflare 100s 上限），且每块有 90s 显式超时，不等 CF 切断。
        self.assertIn("CHUNK_FIRST", response.text)
        self.assertIn("xhr.timeout", response.text)

    def test_raw_and_download_stream_large_local_files(self):
        """5MB 本地文件经 /raw 和 /download 流式回传：内容完整，带长度头，
        且服务端不再整包读进内存（本地以前是 read 全量）。"""
        import hashlib
        import tempfile

        self.login()
        data = bytes(range(256)) * (5 * 1024 * 1024 // 256)
        total = len(data)
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            offset = 0
            while offset < total:
                chunk = data[offset:offset + 1024 * 1024]
                response = self.client.put(
                    f"/api/panes/%254/files/upload?name=big.bin&dir={directory}"
                    f"&offset={offset}&total={total}&token=abc", content=chunk)
                self.assertEqual(response.status_code, 200, response.text)
                offset += len(chunk)
            for url in (f"/api/panes/%254/files/raw?path={directory}/big.bin",
                        f"/api/panes/%254/files/download?path={directory}/big.bin"):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertEqual(response.headers.get("content-length"), str(total))
                self.assertEqual(hashlib.sha256(response.content).hexdigest(),
                                 hashlib.sha256(data).hexdigest())

    def test_single_shot_upload_over_16mb_is_rejected(self):
        """total=0 的整包上传超过 16MB 才 413：慢链路上整包必撞 CF 上限，大文件
        必须走分块；16MB 以内的整包仍然放行，免得还没升级的远端 relay 直接失败。"""
        import tempfile

        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            response = self.client.put(
                f"/api/panes/%254/files/upload?dir={directory}&name=big.bin",
                content=b"z" * (17 * 1024 * 1024))
            self.assertEqual(response.status_code, 413)
            self.assertIn("chunked", response.json()["detail"])
            # 4-16MB 之间：老 relay 的整包上传仍然能用
            allowed = self.client.put(
                f"/api/panes/%254/files/upload?dir={directory}&name=relay.bin",
                content=b"z" * (5 * 1024 * 1024))
            self.assertEqual(allowed.status_code, 200)
            small = self.client.put(
                f"/api/panes/%254/files/upload?dir={directory}&name=small.bin",
                content=b"ok")
            self.assertEqual(small.status_code, 200)

    def test_a_shell_pane_has_no_model_picker(self):
        self.login()
        self.tmux.panes[0].update({"command": "zsh", "provider": None})
        self.assertEqual(self.client.get("/api/panes/%254/models").status_code, 422)
        self.assertEqual(self.client.post("/api/panes/%254/model", json={"model": "opus"}).status_code, 422)

    def test_deletes_a_single_pane_and_keeps_the_session(self):
        self.login()
        self.tmux.panes.append({**self.tmux.panes[0], "id": "%5", "target": "codex:0.1",
                                "window": "0", "window_name": "shell", "command": "zsh"})
        response = self.client.delete("/api/panes/%254")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["pane"], "%4")
        self.assertEqual(body["session"], "codex")
        self.assertTrue(body["session_alive"], "同会话还有别的 pane，会话不能一起没")
        self.assertEqual([pane["id"] for pane in self.tmux.panes], ["%5"])

    def test_deleting_the_last_pane_says_the_session_is_gone(self):
        self.login()
        response = self.client.delete("/api/panes/%254")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["session_alive"])
        self.assertEqual(self.client.delete("/api/panes/%254").status_code, 404)

    def test_deletes_a_local_session(self):
        self.login()
        response = self.client.delete("/api/sessions/codex")   # FakeTmux 里唯一的会话
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["killed"], True)
        self.assertNotIn("codex", {pane["session"] for pane in self.tmux.panes})
        self.assertEqual(self.client.delete("/api/sessions/codex").status_code, 404)

    def test_deletes_a_remote_session(self):
        host_key = ("10.0.0.6 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l"
                    "+Kg3QGMCbo2")
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        host = self.client.post("/api/remote-hosts", json={
            "name": "kill-box", "hostname": "10.0.0.6", "port": 22, "username": "ubuntu",
            "auth_type": "agent", "credential": "", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        response = self.client.delete(f"/api/sessions/old-agent?host={host['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FakeSSHRunner.killed, "old-agent")
        self.assertEqual(self.client.delete("/api/sessions/old?host=deadbeefdeadbeef").status_code, 404)

    def test_creates_a_session_on_a_remote_host(self):
        host_key = ("10.0.0.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l"
                    "+Kg3QGMCbo2")
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        host = self.client.post("/api/remote-hosts", json={
            "name": "session-box", "hostname": "10.0.0.5", "port": 22, "username": "ubuntu",
            "auth_type": "agent", "credential": "", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        response = self.client.post("/api/sessions", json={"host": host["id"], "name": "remote-new",
                                                           "path": "/srv/work"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(FakeSSHRunner.created, ("remote-new", "/srv/work"))
        self.assertEqual(body["host"], host["id"])
        self.assertEqual([pane["id"] for pane in body["panes"]], [pane_ref(host["id"], "%11")])

    def test_refreshing_one_host_leaves_the_others_alone(self):
        """The sidebar's per-host refresh must not re-probe every remote."""
        self.login()
        self.client.get("/api/hosts?refresh=1")   # snapshot exists now
        calls = {"n": 0}
        original = FakeSSHRunner.list_panes

        def counting(runner, *args, **kwargs):
            calls["n"] += 1
            return original(runner, *args, **kwargs)

        with mock.patch.object(FakeSSHRunner, "list_panes", counting):
            local = self.client.get("/api/hosts", params={"refresh": 1, "host": "localhost"})
            self.assertEqual(local.status_code, 200)
            self.assertEqual(calls["n"], 0, "a localhost refresh must not touch remote hosts")
            hosts = {host["id"]: host for host in local.json()["hosts"]}
            self.assertTrue(hosts["localhost"]["panes"])

            calls["n"] = 0
            board = self.client.get("/api/hosts", params={"refresh": 1, "host": "localhost"})
            self.assertEqual(calls["n"], 0)
            # an unknown host falls back to a full snapshot instead of failing
            unknown = self.client.get("/api/hosts", params={"refresh": 1, "host": "nope"})
            self.assertEqual(unknown.status_code, 200)


        host_key = ("10.0.0.7 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l"
                    "+Kg3QGMCbo2")
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        response = self.client.post("/api/remote-hosts", json={
            "name": "agent-box", "hostname": "10.0.0.7", "port": 22, "username": "ubuntu",
            "auth_type": "agent", "credential": "", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        })
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["auth_type"], "agent")
        self.assertNotIn("credential", body)
        listing = self.client.get("/api/remote-hosts").json()["hosts"]
        self.assertTrue(any(host["name"] == "agent-box" for host in listing))

    def test_password_or_key_auth_still_requires_a_credential(self):
        host_key = ("10.0.0.6 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l"
                    "+Kg3QGMCbo2")
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        for auth_type in ("password", "key"):
            response = self.client.post("/api/remote-hosts", json={
                "name": "empty", "hostname": "10.0.0.6", "port": 22, "username": "ubuntu",
                "auth_type": auth_type, "credential": "", "host_key": host_key,
                "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
            })
            self.assertEqual(response.status_code, 422, auth_type)

    def test_zip_works_for_a_remote_pane(self):
        host_key = ("10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l"
                    "+Kg3QGMCbo2")
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        host = self.client.post("/api/remote-hosts", json={
            "name": "archive-box", "hostname": "10.0.0.9", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()
        remote_pane = pane_ref(host["id"], "%9")
        response = self.client.post(f"/api/panes/{quote(remote_pane, safe='')}/files/zip",
                                    json={"base": "/srv/work", "paths": ["/srv/work/one.txt"]})
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(archive.namelist(), ["remote.txt"])
        paths, base, _max_bytes, _max_files = FakeSSHRunner.zipped
        self.assertEqual(paths, ("/srv/work/one.txt",))
        self.assertEqual(base, "/srv/work")

    def test_one_card_handles_notices_and_manual_inspection(self):
        self.login()
        html = self.client.get("/").text
        self.assertEqual(html.count('id="prompt-card"'), 1)
        self.assertNotIn('id="needs-you-card"', html)
        self.assertIn("TmuxPrompt.start", self.console_surface())

    def test_the_prompt_endpoint_needs_a_login(self):
        for path in ("/api/panes/%254/prompt",):
            response = self.client.get(path, follow_redirects=False)
            self.assertIn(response.status_code, (303, 401, 403), path)
        response = self.client.post("/api/panes/%254/prompt/choose", json={"index": 0},
                                    follow_redirects=False)
        self.assertIn(response.status_code, (303, 401, 403))

    def test_legacy_pref_and_saved_grant_do_not_enable_screen_actions(self):
        self.enable_prompt_card()
        self.tmux.output = self.NEEDS_MENU
        path = "/api/panes/%254/prompt"
        from agent_prompts import menu_signature, parse_screen
        grant = self.client.post("/api/panes/%254/approvals", json={
            "signature": menu_signature(parse_screen(self.tmux.output)), "scope": "always",
            "action": {"index": 0}, "label": "Yes"})
        self.assertEqual(grant.status_code, 200)
        self.addCleanup(self.client.delete, "/api/panes/%254/approvals/" + grant.json()["id"])
        self.assertTrue(self.client.get(path).json()["disabled"])
        prompt = self.client.get(path, params={"inspect": True}).json()
        self.assertTrue(prompt["screen"])
        self.assertIsNone(prompt["prompt"])
        self.assertNotIn("grant", prompt)
        for body in ({"index": 0}, {"key": "Enter", "revision": "legacy"},
                     {"toggle": 0, "value": 1, "revision": "legacy"}):
            self.assertEqual(self.client.post(path + "/choose", json=body).status_code, 409)
        self.assertEqual(self.tmux.keys, [])

    def test_codex_command_approval_preview_requires_the_live_footer_and_cannot_send(self):
        self.login()
        pane = self.tmux.panes[0]
        pane.update(provider="codex", pane_pid="123")
        cmd = "bash /tmp/tw-live-color-rollout.41pw8qeg/rollout.sh"
        text = (f"$ {cmd}\n› 1. Yes, proceed (y)\n"
                f"  2. Yes, and don't ask again for commands that start with `{cmd}`\n"
                "     (p)\n  3. No, and tell Codex what to do differently (esc)\n"
                "  Press enter to confirm or esc to cancel")
        self.tmux.output = text
        self.tmux.capture_ansi = lambda *a, **kw: "\x1b[32m" + self.tmux.output + "\x1b[0m"
        tail = "/api/panes/%254/tail?lines=12&color=1"
        self.assertTrue(self.client.get(tail).json()["codex_command_approval"])
        path = "/api/panes/%254/prompt"
        card = self.client.get(path + "?inspect=true").json()["prompt"]
        self.assertTrue(card["read_only"])
        self.assertEqual(card["options"][0]["description"], "$ " + cmd)
        self.assertEqual(len(card["options"]), 3)
        self.assertEqual(card["options"][1]["description"], cmd)
        self.assertEqual(self.client.post(path + "/choose", json={"revision": card["revision"], "index": 1}).status_code, 409)
        self.assertFalse(self.tmux.keys)
        self.assertIsNone(tmux_web.native_interaction(pane, text, []))
        framed = "Would you like to run the following command?\nEnvironment: local\nReason: deploy\n" + text
        notice = tmux_web.native_interaction(pane, framed, [])
        self.assertTrue(notice["read_only"])
        self.assertEqual(notice["options"], [])
        self.tmux.output = framed
        self.assertEqual(self.client.get(tail).json()["codex_screen_notice"], notice)
        inbox = "/api/needs-you/inbox"
        self.client.get(tail)
        rows = self.client.get(inbox).json()["items"]
        self.assertEqual([(item["pane_id"], item["summary"], item["pane_alive"]) for item in rows],
                         [("%4", notice["title"], True)])
        self.tmux.output = text  # A partial frame cannot dismiss the preceding complete one.
        self.client.get(tail)
        self.assertEqual(self.client.get(inbox + "/count").json()["open"], 1)
        self.tmux.output = framed
        self.assertEqual(self.client.post(path + "/choose", json={"revision": notice["revision"], "index": 0}).status_code, 422)
        self.assertFalse(self.tmux.keys)
        hook = [{"title": "等待工具审批", "id": ["session", "request"]}]
        self.assertEqual(tmux_web.native_interaction(pane, text, hook)["source"], "codex_command_approval")
        for pending in (hook, hook * 2, [dict(hook[0], detail="command-sha256:other")]):
            self.assertEqual(tmux_web.native_interaction(pane, framed, pending), notice)
        for screen in (text + "\n› Ask Codex to do anything", text.replace("(p)", ""),
                       text.replace("› 1.", "1."), text.replace("  3.", "› 3.")):
            self.assertIsNone(tmux_web.native_interaction(pane, screen, [], inspect_screen=True))
        self.assertIsNone(tmux_web.native_interaction(dict(pane, provider="kimi"), text, [], inspect_screen=True))
        self.tmux.output += "\n› Ask Codex to do anything"
        self.assertNotIn("codex_command_approval", self.client.get(tail).json())
        self.assertEqual(self.client.get(inbox + "/count").json()["open"], 0)
        history = self.client.get(inbox, params={"history": 1}).json()["items"]
        self.assertEqual([(item["id"], item["resolved_reason"]) for item in history],
                         [(rows[0]["id"], "cleared")])

    def test_ssh_approval_reads_owning_hook_and_rechecks_before_submission(self):
        import shlex
        from plugins.agent.hooks.observe import record
        from remote_hosts import SSHRunner

        self.login()
        host = self.remote_store.add_host(name="remote", hostname="test", username="user", credential="")
        pane = dict(self.tmux.panes[0], pane_id="%4", pane_pid="135", host_id=host["id"],
                    id=pane_ref(host["id"], "%4"), provider="codex")
        screen = ("Would you like to run the following command?\n$ example task\n"
                  "› 1. Yes, proceed (y)\n  2. No, and tell Codex what to do differently (esc)"
                  "\n  Press enter to confirm or esc to cancel")
        url = "/api/panes/" + quote(pane["id"], safe="")
        detail = "command-sha256:" + hashlib.sha256(b"example task").hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / ".config/tmuxweb-agent/interactions.sqlite3"
            def observe(pid, request, action="open"):
                record(store, "%4", pid, "codex", ("s", request, action,
                       "等待工具审批" if action == "open" else "", detail), time.time())
            def run(_runner, cmd, **kwargs):
                return subprocess.run(shlex.split(cmd), env=dict(os.environ, HOME=folder),
                                      capture_output=True, timeout=kwargs["timeout"])
            with (patch.object(FakeSSHRunner, "list_panes", return_value=[pane]),
                  patch.object(FakeSSHRunner, "screen", screen),
                  patch.object(FakeSSHRunner, "command_approval_notices", SSHRunner.command_approval_notices, create=True),
                  patch.object(FakeSSHRunner, "run", run),
                  patch.object(FakeSSHRunner, "send_keys", create=True) as send):
                tail = lambda: self.client.get(url + "/tail").json()
                self.assertNotIn("codex_verified_approval", tail())
                self.assertFalse(store.exists())
                observe("134", "wrong-process")
                self.assertNotIn("codex_verified_approval", tail())
                observe("135", "request")
                live = tail()["codex_verified_approval"]
                action = {"index": 0, "revision": live["revision"]}
                self.assertFalse(live["read_only"])
                observe("135", "second")
                self.assertNotIn("codex_verified_approval", tail())
                self.assertEqual(self.client.post(url + "/prompt/choose", json=action).status_code, 409)
                send.assert_not_called()
                observe("135", "second", "close")
                self.assertEqual(self.client.post(url + "/prompt/choose", json=action).status_code, 200)
                self.assertEqual(send.call_args.args, ("%4", ["y"]))
                self.assertEqual(self.tmux.keys, [])

    def test_codex_approval_requires_the_same_native_command_at_click_time(self):
        from plugins.agent.hooks.observe import observation
        self.login()
        self.tmux.panes[0].update(provider="codex", pane_pid="123")
        self.tmux.output = ("$ example task\n› 1. Yes, proceed (y)\n"
                            "  2. Yes, and don't ask again for commands that start with `example` (p)\n"
                            "  3. No, and tell Codex what to do differently (esc)\n  Press enter to confirm or esc to cancel")
        event = {"session_id": "session", "tool_use_id": "a", "tool_name": "Bash",
                 "tool_input": {"command": "example task"}}

        def pending(panes, timeout):
            session, request, _, title, detail = observation("codex", "PermissionRequest", event)
            return {"%4": {"notices": [{"id": [session, request], "title": title, "detail": detail}]}}

        tail = "/api/panes/%254/tail?lines=12"
        other = pending([], 2)["%4"]
        self.tmux.needs_input_many = lambda panes, timeout: {"%170": other}
        self.assertNotIn("codex_verified_approval", self.client.get(tail).json())
        self.tmux.needs_input_many = pending
        self.assertTrue(self.client.get(tail).json()["codex_verified_approval"]["verified"])
        path = "/api/panes/%254/prompt"
        def preview():
            return self.client.get(path + "?inspect=true&fresh=true").json()["prompt"]

        def choose(card, index):
            return self.client.post(path + "/choose", json={"index": index, "revision": card["revision"]})

        card = preview()
        self.assertTrue(card["verified"])
        self.assertTrue(card["options"][1]["native_only"])
        self.assertEqual(choose(card, 1).status_code, 422)
        event["tool_input"]["command"] = "another command"
        self.assertTrue(preview()["read_only"])
        self.assertEqual(choose(card, 0).status_code, 409)
        self.assertEqual(self.tmux.keys, [])
        event["tool_input"]["command"] = "example task"
        self.assertEqual(choose(card, 0).json()["sent"], ["y"])
        self.assertEqual(choose(card, 0).status_code, 409)
        event["tool_use_id"] = "next"
        self.assertEqual(choose(card, 2).status_code, 409)
        self.assertEqual(choose(preview(), 2).json()["sent"], ["Escape"])
        event.pop("tool_use_id")
        self.assertTrue(preview()["read_only"])

    def test_hermes_single_question_uses_one_digit_and_rechecks_the_live_pane(self):
        self.login()
        self.tmux.panes[0].update(provider="hermes", pane_pid="123")
        screen = ("╭─ Hermes Agent needs your input ─────╮\n│ 1 question │\n"
                  "│ ▸ 你现在用哪种设备查看 Chat？ │\n│ ❯ 1. 手机 (Recommended) │\n"
                  "│   2. 电脑 │\n│   3. Other (type your answer) │\n╰─────╯\n"
                  "↑/↓ to select, Enter to lock, Tab next question  (100s)\n"
                  "── ❓ 你现在用哪种设备查看 Chat？ ──\n? ❯\n─────\nHermes")
        notices = [{"id": ["s", "r1"], "title": "需要回答问题", "detail": "你现在用哪种设备查看 Chat？"}]
        self.tmux.needs_input_many = lambda panes, timeout: {"%4": {"notices": notices}}
        self.tmux.output = screen
        path = "/api/panes/%254/prompt"
        prompt = self.client.get(path, params={"inspect": True}).json()["prompt"]
        states = self._wait_for_panes(self._fresh_status_client(), lambda rows: rows[0]["needs_input"])
        self.assertTrue(states[0]["prompt"]["verified"])
        self.assertEqual(states[0]["prompt"]["revision"], prompt["revision"])
        self.assertEqual(prompt["source"], "hermes_clarify")
        self.assertEqual([o["label"] for o in prompt["options"]], ["手机 (Recommended)", "电脑", "Other (type your answer)"])
        self.assertTrue(prompt["options"][-1]["opens_input"])
        sent = []
        self.tmux.send_keys = lambda pane_id, keys, pane=None: sent.extend(keys)
        choice = {"index": 1, "revision": prompt["revision"]}
        for bad in ([], [dict(notices[0], id=["s", "new"])],
                    [dict(notices[0], title="等待操作审批")], [dict(notices[0], detail="另一个问题")],
                    [*notices, dict(notices[0], id=["s", "another"])]):
            original = notices
            notices = bad
            self.assertEqual(self.client.post(path + "/choose", json=choice).status_code, 409)
            notices = original
        for stale in (screen.replace("电脑", "平板"), screen.replace("? ❯", "❯ Draft"),
                      screen.replace("1 question", "2 questions"), screen.replace("❯ 1.", "❯ [ ] 1.")):
            self.tmux.output = stale
            self.assertEqual(self.client.post(path + "/choose", json=choice).status_code, 409)
        self.tmux.output = screen
        self.tmux.panes[0]["pane_pid"] = "456"
        self.assertEqual(self.client.post(path + "/choose", json=choice).status_code, 409)
        self.tmux.panes[0]["pane_pid"] = "123"
        self.assertEqual(self.client.post(path + "/choose", json={**choice, "key": "enter"}).status_code, 422)
        self.assertEqual(sent, [])
        self.assertEqual(self.client.post(path + "/choose", json=choice).status_code, 200)
        self.assertEqual(self.client.post(path + "/choose", json=choice).status_code, 409)
        self.assertEqual(sent, ["2"])

    def test_hermes_batch_choice_is_bound_to_the_expanded_question(self):
        self.login()
        self.client.put("/api/prefs", json={"prompt_card": "off"})
        self.tmux.panes[0].update(provider="hermes", pane_pid="123")
        self.tmux.needs_input_many = lambda panes, timeout: {"%4": {"notices": [
            {"id": ["s", "r1"], "title": "需要回答问题", "detail": "Provider?\nModel?"}]}}
        first = ["▸ Provider?", " ❯ 1. openai", "   2. claude", "   3. Other (type your answer)", "· Model?"]
        second = ["✓ Provider?", "    claude", "▸ Model?", " ❯ 1. Default", "   2. Other (type your answer)"]
        def frame(rows):
            middle = "\n".join("│ " + row + " │" for row in ["2 questions", *rows])
            return ("╭─ Hermes Agent needs your input ─╮\n" + middle + "\n╰───╯\n"
                    "↑/↓ to select, Enter to lock, Tab next question (80s)\n── ❓ ──\n? ❯\n───")
        self.tmux.output = frame(first)
        seen = []
        self.tmux.send_keys = lambda pane_id, keys, pane=None: seen.extend(keys)
        path = "/api/panes/%254/prompt"
        def inspect():
            return self.client.get(path, params={"inspect": True}).json()["prompt"]
        def act(prompt, **action):
            return self.client.post(path + "/choose", json={"revision": prompt["revision"], **action}).status_code
        provider = inspect()
        self.assertEqual(provider["question_count"], 2)
        self.assertEqual(act(provider, key="Enter"), 422)
        self.assertEqual(act(provider, key="Tab"), 200)
        self.tmux.output = frame(second)
        self.assertEqual(act(provider, index=1), 409)
        model = inspect()
        self.assertEqual(model["title"], "Model?")
        self.assertEqual(act(model, key="BTab"), 200)
        self.tmux.output = frame(first)
        self.assertEqual(act(provider, index=1), 200)
        self.tmux.output = frame(second)
        self.assertEqual(act(model, index=0), 200)
        self.assertEqual(seen, ["Tab", "BTab", "2", "1"])
        self.tmux.output = frame(first[:-1])
        self.assertTrue(inspect()["read_only"])

    def test_checklists_are_plain_screen_text_even_after_enabling_the_old_preference(self):
        self.enable_prompt_card()
        self.tmux.output = "Select tools?\n  ❯ [ ] Shell\n    [x] Search\n↑↓ navigate · Space toggle · Enter submit"
        path = "/api/panes/%254/prompt"
        data = self.client.get(path, params={"inspect": True}).json()
        self.assertIsNone(data["prompt"])
        self.assertIn("Shell", "\n".join(data["screen"]))
        self.assertEqual(self.client.post(path + "/choose", json={"index": 0, "revision": "old"}).status_code, 409)

    def test_parallel_prompt_inspections_share_one_capture(self):
        import asyncio
        import httpx

        self.login()
        original_capture = self.tmux.capture
        captures = []

        def capture(*args, **kwargs):
            captures.append(args[0])
            time.sleep(0.08)
            return original_capture(*args, **kwargs)

        self.tmux.capture = capture

        async def inspect():
            transport = httpx.ASGITransport(app=self.client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                         cookies=self.client.cookies) as client:
                return await asyncio.gather(*[
                    client.get("/api/panes/%254/prompt?inspect=true")
                    for _ in range(24)
                ])

        responses = asyncio.run(inspect())
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(captures, ["%4"])

    def test_manual_inspection_returns_raw_screen_without_inventing_choices(self):
        self.login()
        self.tmux.output = "unrecognized TUI screen\n"
        data = self.client.get("/api/panes/%254/prompt?inspect=true").json()
        self.assertIsNone(data["prompt"])
        self.assertEqual(data["screen"], ["unrecognized TUI screen"])

    def test_no_menu_means_no_buttons(self):
        self.login()
        self.tmux.output = "hello\n❯ Ask Codex to do anything\n"
        payload = self.client.get("/api/panes/%254/prompt").json()
        self.assertIsNone(payload["prompt"])
        self.assertEqual(payload["screen"], [])


    def test_the_preference_rejects_unknown_values(self):
        self.login()
        bad = self.client.put("/api/prefs", json={"prompt_card": "maybe"})
        self.assertEqual(bad.status_code, 400)

    def test_old_clients_read_the_retired_preference_as_off(self):
        self.enable_prompt_card()
        self.assertEqual(self.client.get("/api/prefs").json()["prefs"]["prompt_card"], "off")
        self.assertNotIn("data-ts-prompt", self.client.get("/assets/settings.js").text)

    def test_sending_hides_the_attachment_strip_right_away(self):
        """按 Enter 那一刻条带就该收起来，而不是等 POST 回来（那一秒图片还杵着）。"""
        self.login()
        page = self.console_surface()
        send = page[page.index("async function sendInput("):]
        send = send[:send.index("async function createShare(")]
        self.assertIn("prompt?takeAttachments():[]", send, "先收条带")
        self.assertLess(send.index("prompt?takeAttachments():[]"),
                        send.index("/input"), "要在发请求之前收")
        self.assertNotIn("clearAttachments()", send, "不能再等成功后清")
        # 失败也不回填：消息已经画在聊天里（带重发），再放回输入框会让人重复发一遍
        self.assertNotIn("restoreAttachments(attachments)", send)
        self.assertNotIn("input.value=typed", send)
        self.assertIn("点消息上的重发", send)

    def test_composer_files_can_take_and_restore_the_strip(self):
        script = (Path(__file__).resolve().parents[1] / "web" / "assets" / "composer-files.js").read_text()
        for name in ("function take()", "function restore(taken)", "function discard(taken)"):
            self.assertIn(name, script)
        self.assertIn("take, restore, discard", script)

    def test_exec_runs_a_command_and_returns_its_output(self):
        """给远程调试的 agent：一次调用拿到 stdout/stderr/退出码。"""
        self.login()
        ok = self.client.post("/api/exec", json={"command": "echo hello; echo oops >&2",
                                                 "cwd": "/tmp"})
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertEqual(body["exit"], 0)
        self.assertIn("hello", body["stdout"])
        self.assertIn("oops", body["stderr"])
        self.assertFalse(body["truncated"])

        bad = self.client.post("/api/exec", json={"command": "exit 3"}).json()
        self.assertEqual(bad["exit"], 3, "退出码要原样带回来")

    def test_exec_honours_the_output_cap(self):
        self.login()
        body = self.client.post("/api/exec", json={"command": "printf 'x%.0s' $(seq 1 5000)",
                                                   "max_output": 1000}).json()
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["stdout"]), 1000)

    def test_exec_times_out_instead_of_hanging(self):
        self.login()
        body = self.client.post("/api/exec", json={"command": "sleep 5", "timeout": 1}).json()
        self.assertEqual(body["exit"], 124)
        self.assertIn("timeout", body["stderr"])

    def test_exec_works_with_the_api_token(self):
        """muse 这种远程 agent 用的是 token，不是登录 cookie。"""
        token = "unit-test-api-token"
        import os
        os.environ["TMUX_WEB_API_TOKEN"] = token
        try:
            client = TestClient(create_app(
                FakeTmux(), password="unit-test-admin-password", session_secret="test-secret",
                terminal_factory=FakeTerminalFactory(), share_db=":memory:",
                remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner))
            anonymous = client.post("/api/exec", json={"command": "echo hi"})
            self.assertEqual(anonymous.status_code, 401)
            allowed = client.post("/api/exec", json={"command": "echo hi"},
                                  headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(allowed.status_code, 200)
            self.assertIn("hi", allowed.json()["stdout"])
        finally:
            os.environ.pop("TMUX_WEB_API_TOKEN", None)

    def test_mcp_run_command_matches_the_rest_endpoint(self):
        """agent 走 MCP 也要能一次拿到输出，而不是只走 REST。"""
        self.login()
        listing = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertIn("run_command", names)

        call = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "run_command",
                       "arguments": {"command": "echo mcp-ok; exit 0", "timeout": 10}}}).json()
        payload = json.loads(call["result"]["content"][0]["text"])
        self.assertEqual(payload["exit"], 0)
        self.assertIn("mcp-ok", payload["stdout"])
        self.assertFalse(call["result"].get("isError"))

    def mcp_call(self, name, arguments):
        """tools/call 一次调用，返回 (解析后的 content, isError)。"""
        response = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}).json()
        text = response["result"]["content"][0]["text"]
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        return payload, bool(response["result"].get("isError"))

    def test_project_memberships_shared_between_rest_and_mcp(self):
        self.assertEqual(self.client.post("/api/pane-projects", json={"project":"empty"}).status_code, 401)
        self.login()
        for name in ("QA", "Dev"):
            result, error = self.mcp_call("create_project", {"project": name})
            self.assertFalse(error)
            self.assertIn(name, self.client.get("/api/pane-projects").json()["names"])
        self.assertTrue(self.mcp_call("join_project", {"project":"QA"})[1])
        self.assertTrue(self.mcp_call("join_project", {"caller":"%4", "project":"missing"})[1])
        self.assertFalse(self.mcp_call("join_project", {"caller":"%4", "project":"QA"})[1])
        response = self.client.patch("/api/panes/%254/projects", json={"add":["Dev"]})
        self.assertEqual(response.json()["projects"], ["Dev","QA"])
        listing = self.mcp_call("list_projects", {})[0]["projects"]
        self.assertTrue(all({"name": n, "members": ["%4"]} in listing for n in ("Dev", "QA")))
        self.assertEqual(self.mcp_call("leave_project", {"caller":"%4", "project":"QA"})[0]["projects"], ["Dev"])
        self.assertTrue(self.mcp_call("remove_project_member", {"project":"Dev"})[1])
        self.assertFalse(self.mcp_call("remove_project_member", {"target":"%4", "project":"Dev"})[1])
        self.assertNotIn("%4", self.client.get("/api/pane-projects").json()["memberships"])
        self.assertFalse(self.tmux.sent or self.tmux.keys)

    def test_mcp_create_session_matches_the_rest_endpoint(self):
        """走 /mcp 也要能建会话：和 POST /api/sessions 同一条后端。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            payload, is_error = self.mcp_call(
                "create_session", {"name": "mcp-dev", "path": directory})
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["session"], "mcp-dev")
        self.assertEqual(payload["host"], "localhost")
        self.assertEqual(payload["pane"], "%12")
        self.assertEqual(self.tmux.created, [("mcp-dev", directory)])

    def test_mcp_create_session_rejects_a_duplicate_name(self):
        """重名走 409：和 REST 的错误映射一致。"""
        self.login()
        payload, is_error = self.mcp_call("create_session", {"name": "codex"})
        self.assertTrue(is_error)
        self.assertIn("409", payload["error"])
        self.assertIn("already exists", payload["error"])

    def test_mcp_create_session_rejects_an_uninstalled_agent(self):
        """目标主机上没装的 agent 走 422，不建会话。"""
        self.login()
        payload, is_error = self.mcp_call(
            "create_session", {"name": "mcp-agent", "agent": "definitely-not-an-agent"})
        self.assertTrue(is_error)
        self.assertIn("422", payload["error"])
        self.assertIn("agent not installed", payload["error"])
        self.assertEqual(self.tmux.created, [])

    def test_mcp_create_session_starts_an_installed_agent(self):
        """已安装的 agent 按探测到的绝对路径启动，参数原样传过去。"""
        self.login()
        with mock.patch("agent_catalog.probe_local",
                        return_value=[{"name": "hermes", "path": "/usr/local/bin/hermes"}]):
            payload, is_error = self.mcp_call(
                "create_session", {"name": "mcp-hermes", "agent": "hermes",
                                   "agent_args": "--resume abc"})
        self.assertFalse(is_error, payload)
        self.assertEqual(self.tmux.created_commands[-1],
                         ("mcp-hermes", ["/usr/local/bin/hermes", "--resume", "abc"]))

    def test_mcp_kill_session_closes_one_pane(self):
        """pane 分支只关一个 pane，会话里没 pane 了才算结束。"""
        self.login()
        payload, is_error = self.mcp_call("kill_session", {"pane": "%4"})
        self.assertFalse(is_error, payload)
        self.assertEqual(payload, {"scope": "pane", "pane": "%4", "session": "codex",
                                   "session_alive": False, "killed": True})
        self.assertEqual(self.tmux.panes, [])

    def test_mcp_kill_session_closes_a_whole_session(self):
        """session 分支关掉整个会话。"""
        self.login()
        payload, is_error = self.mcp_call("kill_session", {"session": "codex"})
        self.assertFalse(is_error, payload)
        self.assertEqual(payload["scope"], "session")
        self.assertEqual(payload["session"], "codex")
        self.assertTrue(payload["killed"])
        self.assertEqual(self.tmux.panes, [])
        self.assertIn("codex", self.tmux.killed)

    def test_mcp_kill_session_reports_a_missing_session(self):
        """没有的会话走 404，和 DELETE /api/sessions 一致。"""
        self.login()
        payload, is_error = self.mcp_call("kill_session", {"session": "nope"})
        self.assertTrue(is_error)
        self.assertIn("404", payload["error"])
        self.assertIn("no such session", payload["error"])

    def test_mcp_send_file_writes_through_the_chunked_upload(self):
        """2.5MB 走 3 块 1MB 分块上传：和浏览器上传同一条 .part 机制。"""
        import base64

        self.login()
        data = bytes(range(256)) * 10000
        self.assertGreater(len(data), 2 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            payload, is_error = self.mcp_call(
                "send_file", {"target": "%4", "name": "notes.bin",
                              "content": base64.b64encode(data).decode()})
            self.assertFalse(is_error, payload)
            self.assertEqual(payload["size"], len(data))
            self.assertEqual(payload["path"], os.path.join(directory, "upload_files", "notes.bin"))
            self.assertTrue(payload["notified"])
            self.assertEqual((Path(directory) / "upload_files" / "notes.bin").read_bytes(), data)
            self.assertFalse(list(Path(directory).glob("**/*.part")))
            self.assertIn("[file]", self.tmux.sent[-1][1])
            self.assertIn("notes.bin", self.tmux.sent[-1][1])
            self.assertEqual(self.tmux.sent[-1][1].count("notes.bin"), 1)

    def test_mcp_send_file_writes_an_empty_file(self):
        """空文件走单次上传，不经过分块。"""
        import base64

        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            with patch.object(self.tmux, "send", return_value=False):
                payload, is_error = self.mcp_call(
                    "send_file", {"target": "%4", "name": "empty.txt",
                                  "content": base64.b64encode(b"").decode()})
            self.assertFalse(is_error, payload)
            self.assertEqual(payload["size"], 0)
            self.assertFalse(payload["notified"])
            self.assertEqual((Path(directory) / "upload_files" / "empty.txt").read_bytes(), b"")

    def test_scoped_mcp_file_grant(self):
        import hashlib
        import mcp_file_grants as grants
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.object(grants, "directory", return_value=Path(tmp)/"grants"):
            cwd = Path(tmp)/"target"
            cwd.mkdir()
            self.tmux.panes[0].update(path=str(cwd), pane_pid="105")
            token = grants.issue("%4", "#3", "105", str(cwd))
            self.client.headers.update({"Authorization": "Bearer " + token})
            listed = self.client.post("/mcp", json={"id": 1, "method": "tools/list"}).json()
            self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["send_file"])
            self.assertEqual(self.client.get("/api/panes").status_code, 401)
            args = dict(target="#3", name="doc.bin", content="QUJD")
            for tool, overrides in (("send_agent", {}), ("run_command", {}), ("send_file", {"target":"%5"}),
                                    ("send_file", {"name":"../elsewhere"}), ("send_file", {"notify":True}),
                                    ("send_file", {"path":"/tmp/example"}), ("send_file", {"dir":"/tmp"})):
                self.assertTrue(self.mcp_call(tool, dict(args, **overrides))[1])
            (cwd/"upload_files").symlink_to(Path(tmp), target_is_directory=True)
            self.assertTrue(self.mcp_call("send_file",args)[1])
            (cwd/"upload_files").unlink()
            result, error = self.mcp_call("send_file",args)
            self.assertFalse(error, result)
            self.assertEqual(result["sha256"], hashlib.sha256(b"ABC").hexdigest())
            self.assertFalse(result["notified"])
            self.assertTrue(self.mcp_call("send_file",dict(args,content="REVG"))[1])
            self.assertEqual((cwd/"upload_files/doc.bin").read_bytes(), b"ABC")
            self.tmux.panes[0]["pane_pid"] = "106"
            self.assertTrue(self.mcp_call("send_file",dict(args,name="next.bin"))[1])
            record = next(grants.directory().iterdir())
            record.write_text("null")
            self.assertEqual(self.client.post("/mcp", json={"id":2,"method":"tools/list"}).status_code,401)
            record.unlink()
            self.assertEqual(self.client.post("/mcp", json={"id":2,"method":"tools/list"}).status_code,401)

    def test_mcp_send_file_rejects_a_caller_side_path(self):
        """服务端 /mcp 读不到调用方机器上的文件：必须传 base64 content。"""
        self.login()
        payload, is_error = self.mcp_call(
            "send_file", {"target": "%4", "path": "/etc/passwd"})
        self.assertTrue(is_error)
        self.assertIn("content", payload["error"])

    def test_mcp_send_file_rejects_bad_content(self):
        """不是合法 base64 的 content 直接报错，不写文件。"""
        self.login()
        payload, is_error = self.mcp_call(
            "send_file", {"target": "%4", "name": "x.bin", "content": "!!!not-base64!!!"})
        self.assertTrue(is_error)
        self.assertIn("base64", payload["error"])

    def test_mcp_tools_list_matches_what_the_server_can_execute(self):
        """tools/list 宣称的每个工具都必须能执行：宣称即能执行。

        空参数调一遍：要么成功，要么是参数校验错——但绝不能是 unknown tool。
        """
        self.login()
        listing = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        names = [tool["name"] for tool in listing["result"]["tools"]]
        self.assertEqual(len(names), 21)
        for name in names:
            call = self.client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": name, "arguments": {}}}).json()
            text = call["result"]["content"][0]["text"]
            self.assertNotIn("unknown tool", text, name)

    def test_exec_rejects_unknown_hosts(self):
        self.login()
        response = self.client.post("/api/exec", json={"command": "echo hi", "host": "nope-1234"})
        self.assertEqual(response.status_code, 404)

    def test_an_uploaded_file_can_be_fetched_by_name(self):
        """聊天气泡只留了文件名：按名字到 <pane cwd>/upload_files 里找回来。"""
        import tempfile
        self.login()
        with tempfile.TemporaryDirectory() as folder:
            shots = os.path.join(folder, "upload_files")
            os.makedirs(shots)
            with open(os.path.join(shots, "pasted-1.png"), "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n")
            self.tmux.panes[0]["path"] = folder
            response = self.client.get("/api/panes/%254/uploads/pasted-1.png")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"\x89PNG\r\n\x1a\n")
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertIn("max-age=86400", response.headers["cache-control"])

            missing = self.client.get("/api/panes/%254/uploads/nope.png")
            self.assertEqual(missing.status_code, 404)

            hidden = self.client.get("/api/panes/%254/uploads/.env")
            self.assertEqual(hidden.status_code, 404)

    def test_a_sidecar_thumbnail_is_preferred_for_bubble_previews(self):
        """气泡里的图要 320px 小图：原图按原尺寸解码，十几张就能拖死浏览器。"""
        import tempfile
        self.login()
        with tempfile.TemporaryDirectory() as folder:
            shots = os.path.join(folder, "upload_files")
            os.makedirs(os.path.join(shots, ".thumbs"))
            with open(os.path.join(shots, "big.png"), "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 4096)
            with open(os.path.join(shots, ".thumbs", "big.jpg"), "wb") as handle:
                handle.write(b"\xff\xd8\xff\xe0thumb")
            self.tmux.panes[0]["path"] = folder

            preview = self.client.get("/api/panes/%254/uploads/big.png?thumb=1")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.content, b"\xff\xd8\xff\xe0thumb")
            self.assertEqual(preview.headers["content-type"], "image/jpeg")

            full = self.client.get("/api/panes/%254/uploads/big.png")
            self.assertEqual(full.content[:4], b"\x89PNG", "点开还是原图")

            # 没有 sidecar 就回落到原图，不能 404
            fallback = self.client.get("/api/panes/%254/uploads/big.png?thumb=1") if False else None
            with open(os.path.join(shots, "plain.png"), "wb") as handle:
                handle.write(b"\x89PNGplain")
            again = self.client.get("/api/panes/%254/uploads/plain.png?thumb=1")
            self.assertEqual(again.status_code, 200)
            self.assertEqual(again.content, b"\x89PNGplain")
            self.assertIsNone(fallback)

    def test_uploads_endpoint_needs_a_login(self):
        response = self.client.get("/api/panes/%254/uploads/a.png", follow_redirects=False)
        self.assertIn(response.status_code, (401, 403, 303))

    def test_legacy_card_actions_do_not_send_keys(self):
        self.enable_prompt_card()
        self.tmux.output = "  ←→ to switch · Enter confirm · Esc cancel\n   Thinking\n      Low    [ High ]    Max\n"
        for action in ({"toggle": 0, "value": 2}, {"key": "enter"}, {"key": "esc"}, {"index": 0}):
            response = self.client.post("/api/panes/%254/prompt/choose", json=dict(action, revision="old"))
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.tmux.keys, [])

    def test_a_numbered_list_in_the_chat_is_not_a_menu(self):
        """聊天正文里的 1. 2. 3. 曾被当成菜单，点一下往 pane 里敲了个数字。"""
        import agent_prompts as prompts
        screen = (
            " 回一句是哪个（或直接说要什么）就行。\n"
            "──────────────────────────────\n"
            "─ 12.3s · 2:45 PM\n"
            "──────────────────────────────\n"
            "❯ 1. 是你之前的弹窗选择 我点击了 就发送了1，但是弹窗一直还在。\n"
            "2.  请你把粘贴板的图片直接转附件上传\n"
            "──────────────────────────────\n"
            "☤ deepseek-flash · 408K/1M · [████░░░░░░] 41%\n"
        )
        self.assertIsNone(prompts.parse_screen(screen))

    def test_the_cjk_markdown_fix_is_served_and_wired_into_both_pages(self):
        """**任务：**它 这种写法默认不会渲染成粗体，修正要在聊天和看板都挂上。"""
        script = self.client.get("/assets/markdown-cjk.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("TmuxMarkdownCjk", script.text)
        web = Path(__file__).resolve().parent.parent / "web"
        # 控制台的调用在 console-app.js 里；看板仍在 home.html 里
        app = (web / "assets" / "console-app.js").read_text()
        self.assertIn("TmuxMarkdownCjk.fix(", app, "console-app.js")
        self.assertIn("TmuxMarkdownCjk.strip(", app, "console-app.js")
        home = (web / "home.html").read_text()
        self.assertIn("TmuxMarkdownCjk.fix(", home, "home.html")
        self.assertIn("TmuxMarkdownCjk.strip(", home, "home.html")
        # the console loads it through the bundle now; the board page still has its own tag
        self.assertIn("/assets/console.bundle.js?v=", (web / "index.html").read_text())
        self.assertIn("TmuxMarkdownCjk", self.client.get("/assets/console.bundle.js").text)
        self.assertIn("/assets/markdown-cjk.js", (web / "home.html").read_text())

    def test_the_video_helper_is_served_and_wired_into_the_chat(self):
        """聊天里的视频卡片：脚本要服务得到，页面要引到，样式要在。"""
        script = self.client.get("/assets/video-cards.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("TmuxVideo", script.text)
        self.assertIn("preload = 'metadata'", script.text)
        style = self.client.get("/assets/video-cards.css")
        self.assertEqual(style.status_code, 200)
        self.assertIn(".video-card", style.text)
        # 没登录会被重定向，所以直接读文件里的接线；聊天页和看板共用同一份脚本/样式
        web = Path(__file__).resolve().parent.parent / "web"
        # 控制台的样式走 console.css 打包：index.html 只引打包后的版本化链接
        self.assertIn("/assets/console.css?v=", (web / "index.html").read_text())
        self.assertIn("console.css: video-cards.css",
                      self.client.get("/assets/console.css").text)
        self.assertIn("/assets/video-cards.css", (web / "home.html").read_text())
        self.assertIn("/assets/video-cards.js", (web / "home.html").read_text())
        self.assertIn("/assets/console.bundle.js?v=", (web / "index.html").read_text())
        self.assertIn("TmuxVideo", self.client.get("/assets/console.bundle.js").text)
        self.assertIn("enhanceVideos(root)",
                      (web / "assets" / "console-app.js").read_text())
        self.assertIn("TmuxVideo.enhance($('cards'))", (web / "home.html").read_text())

    def test_a_local_video_is_served_inline_so_a_player_can_use_it(self):
        """<video> 只能就地播；如果 raw 给 attachment，Safari 会直接下载。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "clip.mp4")
            target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            self.tmux.panes[0]["path"] = directory
            response = self.client.get(f"/api/panes/%254/files/raw?path={target}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertIn("inline", response.headers["content-disposition"])

    def test_a_ticket_moves_bytes_without_the_session_cookie(self):
        """直连源站拿不到 cookie：票据本身就是凭据，别的路由不认它。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "notes.md")
            target.write_text("hello 世界")
            self.tmux.panes[0]["path"] = directory
            grant = self.client.post("/api/transfer/ticket",
                                     json={"pane": "%4", "path": str(target),
                                           "direction": "down"}).json()
            self.assertTrue(grant.get("token"))
            stranger = TestClient(self.client.app)          # 一个 cookie 都没有
            response = stranger.get(f"/t/download?t={grant['token']}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content.decode(), "hello 世界")
            # 票据只在 /t/* 有效：带上它去普通路由一样要登录
            self.assertEqual(stranger.get(f"/api/panes/%254/files/download?path={target}").status_code, 401)

    def test_a_ticket_upload_writes_the_file(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            grant = self.client.post("/api/transfer/ticket",
                                     json={"pane": "%4", "path": "/x.bin", "direction": "up",
                                           "size": 5, "name": "x.bin"}).json()
            stranger = TestClient(self.client.app)
            response = stranger.put(
                f"/t/upload?t={grant['token']}&name=x.bin&dir={directory}&offset=0&total=5&token=abc",
                content=b"hello")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Path(directory, "x.bin").read_text(), "hello")

    def test_one_upload_ticket_covers_every_chunk(self):
        """真实上传里块大小会自适应缩小，块数远超事先估算 —— 不能中途 403。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            total = 12 * 100
            grant = self.client.post("/api/transfer/ticket",
                                     json={"pane": "%4", "path": "/many.bin", "direction": "up",
                                           "size": total, "name": "many.bin"}).json()
            stranger = TestClient(self.client.app)
            for offset in range(0, total, 100):
                response = stranger.put(
                    f"/t/upload?t={grant['token']}&name=many.bin&dir={directory}"
                    f"&offset={offset}&total={total}&token=abc",
                    content=b"x" * 100)
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(Path(directory, "many.bin").stat().st_size, total)

    def test_a_zip_ticket_streams_the_archive_without_the_session_cookie(self):
        """打包下载也要能走直连：票据里带 pane + 基准目录 + 选中列表。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a.txt").write_text("aaa")
            Path(directory, "b.txt").write_text("bbb")
            self.tmux.panes[0]["path"] = directory
            grant = self.client.post("/api/transfer/ticket",
                                     json={"pane": "%4", "path": directory, "direction": "zip",
                                           "paths": [str(Path(directory, "a.txt")),
                                                     str(Path(directory, "b.txt"))]}).json()
            self.assertTrue(grant.get("token"))
            stranger = TestClient(self.client.app)
            response = stranger.get(f"/t/zip?t={grant['token']}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "application/zip")
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertTrue(response.content.startswith(b"PK"))

    def test_a_download_ticket_is_not_a_zip_ticket(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "a.txt")
            target.write_text("aaa")
            self.tmux.panes[0]["path"] = directory
            grant = self.client.post("/api/transfer/ticket",
                                     json={"pane": "%4", "path": str(target),
                                           "direction": "down"}).json()
            response = self.client.get(f"/t/zip?t={grant['token']}")
            self.assertEqual(response.status_code, 403)

    def test_a_zip_ticket_needs_paths(self):
        self.login()
        response = self.client.post("/api/transfer/ticket",
                                    json={"pane": "%4", "path": "/tmp", "direction": "zip"})
        self.assertEqual(response.status_code, 422)

    def test_a_download_ticket_cannot_be_used_to_upload(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            grant = self.client.post("/api/transfer/ticket",
                                     json={"pane": "%4", "path": "/x.bin", "direction": "down"}).json()
            response = self.client.put(
                f"/t/upload?t={grant['token']}&name=x.bin&dir={directory}&total=5", content=b"hello")
            self.assertEqual(response.status_code, 403)

    def test_the_ticket_endpoint_needs_a_login(self):
        response = self.client.post("/api/transfer/ticket",
                                    json={"pane": "%4", "path": "/x", "direction": "down"},
                                    follow_redirects=False)
        self.assertIn(response.status_code, (303, 401, 403))

    def test_an_upload_ticket_needs_a_size(self):
        self.login()
        response = self.client.post("/api/transfer/ticket",
                                    json={"pane": "%4", "path": "/x.bin", "direction": "up"})
        self.assertEqual(response.status_code, 422)

    def test_the_ping_answers_cross_origin_without_a_login(self):
        stranger = TestClient(self.client.app)
        response = stranger.get("/t/ping", headers={"Origin": "https://chat.example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"tmuxweb": True})
        self.assertEqual(response.headers["access-control-allow-origin"], "https://chat.example.com")
        preflight = stranger.options("/t/upload", headers={"Origin": "https://chat.example.com"})
        self.assertEqual(preflight.status_code, 204)
        self.assertIn("PUT", preflight.headers["access-control-allow-methods"])

    def test_direct_addresses_are_only_transfer_candidates_over_https(self):
        self.login()
        patcher, _ = self._patch_addresses(serve="https://robo.neko-chroma.ts.net (tailnet only)\n")
        with patcher:
            payload = self.client.get("/api/system/addresses").json()
        by_url = {item["url"]: item for item in payload["addresses"]}
        # MagicDNS 名字来自 tailscale status --json 的 Self.DNSName
        self.assertTrue(by_url["https://robo.neko-chroma.ts.net"]["transfer"])
        self.assertFalse(by_url["http://100.123.123.3:8787"]["transfer"])
        self.assertFalse(by_url["http://10.168.236.31:8787"]["transfer"])

    def test_downloads_a_file_as_an_attachment(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "notes.md")
            target.write_text("hello 世界")
            self.tmux.panes[0]["path"] = directory
            response = self.client.get(f"/api/panes/%254/files/download?path={target}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "hello 世界")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("notes.md", response.headers["content-disposition"])

    def test_raw_endpoint_serves_text_inline_for_preview(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory, "chart.png")
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            text = Path(directory, "notes.md")
            text.write_text("hello 世界")
            self.tmux.panes[0]["path"] = directory
            response = self.client.get(f"/api/panes/%254/files/raw?path={text}")
            picture = self.client.get(f"/api/panes/%254/files/raw?path={image}")
            missing = self.client.get(f"/api/panes/%254/files/raw?path={directory}/gone.png")
            self.assertEqual(self.client.get("/api/panes/%254/files/raw").status_code, 422)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "hello 世界")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertEqual(response.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertEqual(picture.headers["content-type"], "image/png")
        self.assertEqual(missing.status_code, 404)

    def test_raw_endpoint_requires_auth(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            response = self.client.get(f"/api/panes/%254/files/raw?path={handle.name}")
        self.assertIn(response.status_code, (401, 403))

    def test_optimistic_message_reconciliation_is_covered_by_node(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        completed = subprocess.run(
            ["node", "--test", "tests/js/reconcile.test.mjs"],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-2000:] + completed.stderr[-2000:])
        self.assertIn("fail 0", completed.stdout)

    def test_transcript_reports_an_unanswered_turn(self):
        self.login()
        answered = {"provider": "claude", "mtime": 1, "messages": [
            {"id": "u1", "role": "user", "text": "问题", "timestamp": "2026-10-27T22:00:00Z"},
            {"id": "a1", "role": "assistant", "text": "回答", "timestamp": "2026-10-27T22:00:05Z"},
        ]}
        self.tmux.transcript = lambda pane_id, limit=200, *args, **kwargs: dict(answered)
        idle = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertFalse(idle["waiting"])
        waiting = {"provider": "claude", "mtime": 1, "messages": [
            {"id": "u2", "role": "user", "text": "再来一次", "timestamp": "2026-10-27T22:10:00Z"},
        ]}
        self.tmux.transcript = lambda pane_id, limit=200, *args, **kwargs: dict(waiting)
        busy = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertTrue(busy["waiting"])
        self.assertEqual(busy["waiting_since"], "2026-10-27T22:10:00Z")

    def _fake_session_transcript(self, messages, provider="claude"):
        """A fake transcript backed by a real temp session file, so the sig cache works.

        Returns (path, state, fake): mutate state["messages"] and rewrite the file
        to simulate the agent appending, exactly like a real session file growing.
        """
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.write("{}\n")
        handle.close()
        path = handle.name
        self.addCleanup(os.unlink, path)
        state = {"messages": list(messages)}

        def fake(pane_id, limit=200, refresh=False, pane=None):
            return {"pane": pane_id, "session_id": "s1",
                    "mtime": os.path.getmtime(path), "has_more": False,
                    "messages": list(state["messages"]), "path": path,
                    "provider": provider}

        return path, state, fake

    def _get_in_thread(self, url, params=None):
        """Run one client GET in a thread; returns (thread, started_at, result)."""
        result = {}
        started = time.time()

        def run():
            try:
                result["response"] = self.client.get(url, params=params)
            except Exception as exc:  # noqa: BLE001 - surfaced by the caller
                result["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, started, result

    def test_waiting_true_when_last_is_assistant_but_agent_busy(self):
        """首条 assistant 回复到了不等于 turn 结束：session 文件说 BUSY 就还在等。"""
        self.login()
        messages = [
            {"id": "u1", "role": "user", "text": "问题", "timestamp": "2026-10-27T22:00:00Z"},
            {"id": "a1", "role": "assistant", "text": "先回答一部分", "timestamp": "2026-10-27T22:00:05Z"},
        ]
        self.tmux.transcript = lambda pane_id, limit=200, *args, **kwargs: {
            "provider": "claude", "mtime": 1, "messages": list(messages)}
        with patch("tmux_web.agent_status.status_for_pane", return_value="busy"):
            body = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertTrue(body["waiting"])
        # waiting_since 是这一 turn 的开始（最后那条 user 消息），不是 assistant 回复的时间
        self.assertEqual(body["waiting_since"], "2026-10-27T22:00:00Z")

    def test_waiting_false_when_last_is_assistant_and_agent_idle(self):
        self.login()
        messages = [
            {"id": "u1", "role": "user", "text": "问题", "timestamp": "2026-10-27T22:00:00Z"},
            {"id": "a1", "role": "assistant", "text": "回答", "timestamp": "2026-10-27T22:00:05Z"},
        ]
        self.tmux.transcript = lambda pane_id, limit=200, *args, **kwargs: {
            "provider": "claude", "mtime": 1, "messages": list(messages)}
        with patch("tmux_web.agent_status.status_for_pane", return_value="idle"):
            body = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertFalse(body["waiting"])
        self.assertIsNone(body["waiting_since"])

    def test_waiting_true_for_trailing_user_message_even_when_agent_idle(self):
        """最后一条是 user 消息时 waiting 为 true，与 session 文件的 verdict 无关。"""
        self.login()
        self.tmux.transcript = lambda pane_id, limit=200, *args, **kwargs: {
            "provider": "claude", "mtime": 1, "messages": [
                {"id": "u2", "role": "user", "text": "再来一次",
                 "timestamp": "2026-10-27T22:10:00Z"}]}
        with patch("tmux_web.agent_status.status_for_pane", return_value="idle"):
            body = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertTrue(body["waiting"])
        self.assertEqual(body["waiting_since"], "2026-10-27T22:10:00Z")

    def test_stub_poll_returns_fresh_waiting_after_agent_starts(self):
        """stub 路径不能只回缓存的 waiting：agent 由 idle 变 busy 时同一 sig 也要翻转。"""
        self.login()
        messages = [
            {"id": "u1", "role": "user", "text": "问题", "timestamp": "2026-10-27T22:00:00Z"},
            {"id": "a1", "role": "assistant", "text": "先回答一部分",
             "timestamp": "2026-10-27T22:00:05Z"},
        ]
        _path, _state, fake = self._fake_session_transcript(messages)
        calls = []
        orig = fake

        def counting(pane_id, limit=200, refresh=False, pane=None):
            calls.append(pane_id)
            return orig(pane_id, limit, refresh, pane)

        self.tmux.transcript = counting
        with patch("tmux_web.agent_status.status_for_pane", return_value="idle"):
            first = self.client.get("/api/panes/%254/transcript").json()
        self.assertFalse(first["waiting"])
        self.assertEqual(len(calls), 1)
        sig = first["signature"]
        with patch("tmux_web.agent_status.status_for_pane", return_value="busy"):
            second = self.client.get("/api/panes/%254/transcript", params={"sig": sig}).json()
        self.assertEqual(second, {"unchanged": True, "signature": sig, "waiting": True,
                                  "waiting_since": "2026-10-27T22:00:00Z", "has_more": False})
        self.assertEqual(len(calls), 1, "stub 路径不能重读 transcript")
        with patch("tmux_web.agent_status.status_for_pane", return_value="idle"):
            third = self.client.get("/api/panes/%254/transcript", params={"sig": sig}).json()
        self.assertFalse(third["waiting"])
        self.assertEqual(len(calls), 1)

    def test_since_returns_only_new_messages(self):
        """since=最后一条消息 id（或完整 signature）只回该点之后的新消息。"""
        self.login()
        messages = [
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ]
        _path, state, fake = self._fake_session_transcript(messages)
        self.tmux.transcript = fake
        first = self.client.get("/api/panes/%254/transcript").json()
        self.assertEqual(first["signature"], "%s|2|m2" % first["mtime"])
        state["messages"].append(
            {"id": "m3", "role": "assistant", "text": "三", "timestamp": 3})
        # 新消息一定伴随 session 文件变大：只改内存、不动文件时 stat 判定无变化是对的
        Path(_path).write_text("{}\n{}\n{}\n")
        by_id = self.client.get("/api/panes/%254/transcript",
                                params={"since": "m2"}).json()
        self.assertTrue(by_id.get("incremental"))
        self.assertEqual([item["id"] for item in by_id["messages"]], ["m3"])
        self.assertTrue(by_id["signature"].endswith("|3|m3"))
        self.assertEqual(by_id["mtime"], os.path.getmtime(_path))
        by_sig = self.client.get("/api/panes/%254/transcript",
                                 params={"since": first["signature"]}).json()
        self.assertTrue(by_sig.get("incremental"))
        self.assertEqual([item["id"] for item in by_sig["messages"]], ["m3"])
        self.assertEqual(by_sig["signature"], by_id["signature"])

    def test_parallel_transcript_reads_share_one_parse(self):
        import asyncio
        import httpx

        self.login()
        calls = []

        def transcript(pane_id, limit=200, refresh=False, pane=None):
            calls.append((pane_id, limit, refresh))
            time.sleep(0.08)
            return {"provider": "codex", "mtime": 1, "has_more": False,
                    "messages": [{"id": "m1", "role": "user", "text": "question"}]}

        self.tmux.transcript = transcript

        async def fetch():
            transport = httpx.ASGITransport(app=self.client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                         cookies=self.client.cookies) as client:
                return await asyncio.gather(*[
                    client.get("/api/panes/%254/transcript?limit=300")
                    for _ in range(24)
                ])

        results = asyncio.run(fetch())
        self.assertTrue(all(result.status_code == 200 for result in results))
        self.assertEqual(calls, [("%4", 300, False)])

    def test_same_transcript_message_id_can_be_updated(self):
        self.login()
        path, state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "assistant", "text": "partial", "timestamp": 1},
        ])
        self.tmux.transcript = fake
        first = self.client.get("/api/panes/%254/transcript").json()
        state["messages"][0]["text"] = "complete"
        Path(path).write_text("updated record\n")
        body = self.client.get("/api/panes/%254/transcript",
                               params={"since": "m1", "sig": first["signature"]}).json()
        self.assertNotIn("unchanged", body)
        self.assertEqual(body["messages"][0]["text"], "complete")

    def test_since_unknown_returns_full(self):
        """since 对不上当前消息列表（会话轮换）时回全量兜底，而不是空增量。"""
        self.login()
        _path, _state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ])
        self.tmux.transcript = fake
        body = self.client.get("/api/panes/%254/transcript",
                               params={"since": "gone"}).json()
        self.assertNotIn("unchanged", body)
        self.assertFalse(body.get("incremental", False))
        self.assertEqual([item["id"] for item in body["messages"]], ["m1", "m2"])

    def test_since_at_tip_returns_unchanged_stub(self):
        """since 指向最后一条消息说明没有新消息：回 stub，不回空 messages。"""
        self.login()
        _path, _state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ])
        self.tmux.transcript = fake
        first = self.client.get("/api/panes/%254/transcript").json()
        body = self.client.get("/api/panes/%254/transcript",
                               params={"since": "m2"}).json()
        self.assertTrue(body.get("unchanged"))
        self.assertNotIn("messages", body)
        self.assertEqual(body["signature"], first["signature"])

    def test_since_after_rotation_returns_full(self):
        """消息列表整体换掉后，旧 since 不在列表里：回全量。"""
        self.login()
        _path, state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ])
        self.tmux.transcript = fake
        state["messages"] = [
            {"id": "n1", "role": "user", "text": "新一", "timestamp": 10},
            {"id": "n2", "role": "assistant", "text": "新二", "timestamp": 11},
        ]
        body = self.client.get("/api/panes/%254/transcript",
                               params={"since": "m2"}).json()
        self.assertFalse(body.get("incremental", False))
        self.assertEqual([item["id"] for item in body["messages"]], ["n1", "n2"])

    def test_long_poll_wait_is_clamped_to_25(self):
        from tmux_web import clamp_long_poll_wait
        self.assertEqual(clamp_long_poll_wait(99), 25)
        self.assertEqual(clamp_long_poll_wait(25), 25)
        self.assertEqual(clamp_long_poll_wait(10), 10)
        self.assertEqual(clamp_long_poll_wait(0), 0)
        self.assertEqual(clamp_long_poll_wait(-5), 0)
        self.assertEqual(clamp_long_poll_wait(None), 0)

    def test_long_poll_wakes_promptly_when_session_file_changes(self):
        """文件一变，长轮询要在几秒内醒来回新数据，而不是等满 20 秒。"""
        self.login()
        path, state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ])
        self.tmux.transcript = fake
        first = self.client.get("/api/panes/%254/transcript").json()
        thread, started, result = self._get_in_thread(
            "/api/panes/%254/transcript", params={"sig": first["signature"], "wait": 20})
        time.sleep(0.7)
        state["messages"].append(
            {"id": "m3", "role": "assistant", "text": "三", "timestamp": 3})
        Path(path).write_text("{}\n{}\n{}\n")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "长轮询应在文件变化后醒来")
        self.assertNotIn("error", result, result.get("error"))
        elapsed = time.time() - started
        self.assertLess(elapsed, 12, "唤醒延迟 %ss，远大于文件变化后应有的 ~1s" % elapsed)
        body = result["response"].json()
        self.assertNotIn("unchanged", body)
        self.assertEqual(body["messages"][-1]["id"], "m3")

    def test_long_poll_times_out_with_stub_when_nothing_changes(self):
        """无变化时约 wait 秒后超时回 stub（带新鲜 waiting），不是立即回。"""
        self.login()
        _path, _state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ])
        self.tmux.transcript = fake
        first = self.client.get("/api/panes/%254/transcript").json()
        started = time.time()
        body = self.client.get("/api/panes/%254/transcript",
                               params={"sig": first["signature"], "wait": 1}).json()
        elapsed = time.time() - started
        self.assertGreaterEqual(elapsed, 0.8, "wait=1 至少要挂 ~1 秒")
        self.assertLess(elapsed, 8)
        self.assertTrue(body.get("unchanged"))
        self.assertEqual(body["signature"], first["signature"])
        self.assertFalse(body["waiting"])

    def test_long_poll_wakes_when_waiting_flips(self):
        """文件没变、agent 状态翻转（idle→busy）时也要醒来回新鲜 waiting。"""
        self.login()
        messages = [
            {"id": "u1", "role": "user", "text": "问题", "timestamp": "2026-10-27T22:00:00Z"},
            {"id": "a1", "role": "assistant", "text": "先回答一部分",
             "timestamp": "2026-10-27T22:00:05Z"},
        ]
        _path, _state, fake = self._fake_session_transcript(messages)
        self.tmux.transcript = fake
        with patch("tmux_web.agent_status.status_for_pane", return_value="idle"):
            first = self.client.get("/api/panes/%254/transcript").json()
        self.assertFalse(first["waiting"])
        with patch("tmux_web.agent_status.status_for_pane", return_value="busy"):
            thread, started, result = self._get_in_thread(
                "/api/panes/%254/transcript",
                params={"sig": first["signature"], "wait": 20})
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "waiting 翻转应唤醒长轮询")
        self.assertNotIn("error", result, result.get("error"))
        self.assertLess(time.time() - started, 12)
        body = result["response"].json()
        self.assertTrue(body.get("unchanged"))
        self.assertTrue(body["waiting"])
        self.assertEqual(body["waiting_since"], "2026-10-27T22:00:00Z")

    def test_long_poll_is_skipped_for_manual_refresh(self):
        """refresh=1 的手动刷新要立即回，不能被 wait 挂住。"""
        self.login()
        _path, _state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
        ])
        self.tmux.transcript = fake
        started = time.time()
        body = self.client.get("/api/panes/%254/transcript",
                               params={"wait": 20, "refresh": 1}).json()
        self.assertLess(time.time() - started, 5)
        self.assertNotIn("unchanged", body)
        self.assertEqual(len(body["messages"]), 1)

    def test_wait_zero_keeps_old_stub_behaviour(self):
        """wait=0 时行为不变：sig 命中立即回 stub。"""
        self.login()
        _path, _state, fake = self._fake_session_transcript([
            {"id": "m1", "role": "user", "text": "一", "timestamp": 1},
            {"id": "m2", "role": "assistant", "text": "二", "timestamp": 2},
        ])
        self.tmux.transcript = fake
        first = self.client.get("/api/panes/%254/transcript").json()
        started = time.time()
        body = self.client.get("/api/panes/%254/transcript",
                               params={"sig": first["signature"], "wait": 0}).json()
        self.assertLess(time.time() - started, 5)
        self.assertTrue(body.get("unchanged"))

    def test_inline_console_script_passes_node_check(self):
        """web/assets/console-app.js（原 index.html 的内联 module 脚本）必须过 node --check。"""
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        script = Path(__file__).resolve().parent.parent.joinpath(
            "web", "assets", "console-app.js").read_text()
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as handle:
            handle.write(script)
            script_path = handle.name
        self.addCleanup(os.unlink, script_path)
        completed = subprocess.run(["node", "--check", script_path],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])

    def test_incremental_merge_helper_is_covered_by_node(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        completed = subprocess.run(
            ["node", "--test", "tests/js/chat-refresh.test.mjs"],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-2000:] + completed.stderr[-2000:])
        self.assertIn("fail 0", completed.stdout)

    def test_chat_refresh_client_wiring_is_present(self):
        self.login()
        page = self.console_surface()
        for marker in (
            "refreshTranscript(pane,{force:true})",   # 1a：发送后立即刷新
            "since=",                                # 2：增量 transcript
            "wait=20",                               # 3：稳态长轮询
            "transcriptFailures",                    # 1c：失败计数，≥2 次才报
            "mergeIncrementalMessages(",             # 2：增量合并进完整列表
            "long_poll",                             # 3：服务端挂住过才立即再挂
        ):
            self.assertIn(marker, page)

    def test_progress_indicator_markup_is_present(self):
        self.login()
        page = self.console_surface()
        for marker in ('id="agent-progress"', "function syncProgress(", "function sinceSeconds(",
                       "function humanElapsed(", "'thinking'", "'idle'", "function agentWorking(",
                       "agent-progress.working"):
            self.assertIn(marker, page)

    def test_share_and_fullscreen_entries_are_icons_on_phones(self):
        self.login()
        page = self.console_surface()
        # Both share entries use the Octicons share symbol.
        self.assertIn('<a class="icon-btn" href="/shares" title="管理分享"', page)
        self.assertIn('href="/shares" title="管理分享" aria-label="管理分享" style="text-decoration:none"><svg class="ico"><use href="#i-share"/></svg>', page)
        # the fullscreen label lives in a span so the phone layout can hide it
        self.assertIn('<svg class="ico"><use href="#i-fullscreen"/></svg><span>对话全屏</span>', page)
        self.assertIn(".fullscreen-btn span{display:none}", page)
        # the top bar shrinks instead of pushing its buttons off screen
        self.assertIn(".session{flex:1 1 auto;min-width:0}", page)
        self.assertIn(".top-actions{flex:none}", page)

    def test_share_dialog_is_a_fixed_size_sheet_on_phones(self):
        self.login()
        page = self.console_surface()
        # width no longer depends on the containing block, so it cannot jump
        self.assertIn(".share-dialog{width:min(calc(100vw - 24px),520px);max-width:520px;max-height:86dvh;overflow:auto;", page)
        # phones get a bottom sheet with a bounded height and in-place scrolling
        self.assertIn(".share-dialog{width:100vw;max-width:100vw;max-height:88vh;max-height:88dvh;margin:0;inset:auto 0 0 0;", page)
        self.assertIn(".share-grid{grid-template-columns:minmax(0,1fr);gap:9px}", page)
        self.assertIn(".share-actions{position:sticky;bottom:-1px", page)

    def test_html_pages_are_sent_without_http_cache(self):
        login_page = self.client.get("/login", follow_redirects=False)
        self.assertEqual(login_page.headers.get("cache-control"), "no-store")
        self.login()
        for path in ("/", "/shares", "/hosts"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers.get("cache-control"), "no-store", path)

    def test_share_management_buttons_wrap_on_phones(self):
        self.login()
        page = self.client.get("/shares").text
        self.assertIn(".actions button,.actions a{display:inline-flex", page)
        self.assertIn("white-space:nowrap;cursor:pointer", page)
        self.assertIn(".actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}", page)
        self.assertIn("@media(max-width:380px){.actions{grid-template-columns:minmax(0,1fr)}", page)
        self.assertIn(".head{display:flex;flex-wrap:wrap", page)

    def test_transcript_paging_controls_are_present(self):
        self.login()
        page = self.console_surface()
        for marker in ('id="load-earlier"', 'function loadEarlier(', 'TRANSCRIPT_PAGE_MAX',
                       'id="earlier-bar"', 'state.transcriptLimit'):
            self.assertIn(marker, page)
        # The share action is icon-only.
        self.assertIn('<button class="share-btn icon-only" id="share-button"', page)
        self.assertIn('aria-label="分享"><svg class="ico"><use href="#i-share"/></svg>', page)
        self.assertNotIn("</svg>分享", page)

    def test_markdown_links_open_an_in_console_preview(self):
        self.login()
        page = self.console_surface()
        for marker in ('id="md-viewer"', 'id="md-toggle"', 'function openMarkdownViewer(',
                       'function looksLikeMarkdownLink(', 'MD_VIEWER_EXTS'):
            self.assertIn(marker, page)

    def test_rendered_transcript_links_are_rewritten_to_the_raw_endpoint(self):
        self.login()
        page = self.console_surface()
        self.assertIn("/files/raw?path=", page)
        self.assertIn("function rewriteLocalLinks(", page)
        self.assertIn("rewriteLocalLinks(holder)", page)

    def test_uploads_a_file_into_the_current_directory(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            response = self.client.put(
                f"/api/panes/%254/files/upload?dir={directory}&name=drop.bin",
                content=b"\x00payload\xff",
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["size"], len(b"\x00payload\xff"))
            self.assertEqual(Path(directory, "drop.bin").read_bytes(), b"\x00payload\xff")
            self.assertEqual([item.name for item in Path(directory).iterdir()], ["drop.bin"])

    def test_file_endpoints_reject_bad_names_and_oversized_uploads(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            for name in ["..", "a%2Fb", "x%00y"]:
                response = self.client.put(f"/api/panes/%254/files/upload?dir={directory}&name={name}",
                                           content=b"x")
                self.assertEqual(response.status_code, 422, name)
            self.assertEqual(self.client.get("/api/panes/%254/files/download").status_code, 422)
            self.assertEqual(self.client.get("/api/panes/%254/files/download?path=/nope/missing").status_code, 503)

    def test_chat_attachments_land_in_upload_files_and_can_be_removed(self):
        """聊天上传：存到 <pane 目录>/upload_files，用 × 删掉时连磁盘文件一起删。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            target = os.path.join(directory, "upload_files")
            response = self.client.put(
                f"/api/panes/%254/files/upload?dir={target}&name=shot%20one.png",
                content=b"\x89PNG\r\n",
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(Path(target, "shot one.png").is_file())
            removed = self.client.request(
                "DELETE", f"/api/panes/%254/files/upload?dir={target}&name=shot%20one.png")
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertFalse(Path(target, "shot one.png").exists())

    def test_chat_attachment_delete_refuses_anything_outside_upload_files(self):
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            Path(directory, "keep.txt").write_text("keep")
            outside = self.client.request(
                "DELETE", f"/api/panes/%254/files/upload?dir={directory}&name=keep.txt")
            self.assertEqual(outside.status_code, 422)
            self.assertTrue(Path(directory, "keep.txt").exists())
            for name in ["..", "a%2Fb", "x%00y"]:
                bad = self.client.request(
                    "DELETE", f"/api/panes/%254/files/upload?dir={directory}%2Fupload_files&name={name}")
                self.assertEqual(bad.status_code, 422, name)
        self.client.cookies.clear()
        self.assertEqual(self.client.request(
            "DELETE", "/api/panes/%254/files/upload?dir=/tmp/upload_files&name=x").status_code, 401)

    def test_the_chat_composer_can_attach_files(self):
        """控制台聊天和 /preview 共用同一份附件实现（按钮在输入框卡片里，一条细线分隔）。"""
        self.login()
        page = self.console_surface()
        self.assertIn('console.css: composer-files.css', page)
        self.assertIn('console.bundle: assets/composer-files.js',
                      self.client.get("/assets/console.bundle.js").text)
        # 排队条带插在卡片和 cf-bar 之间，所以这里只要求两者相邻关系成立
        self.assertIn('<div class="composer-card">', page)
        self.assertIn('<div class="cf-bar">', page)
        self.assertIn('<div class="cf-strip" id="uploads" hidden></div>', page)
        self.assertIn('id="attach"', page)
        self.assertIn("window.TmuxFiles.mount({", page)
        self.assertIn("const prompt=attachmentsPrompt();", page)

        shared = self.client.get("/assets/composer-files.js").text
        self.assertIn("user uploaded file “${names.join(' & ')}”", shared)
        self.assertIn("/files/upload`", shared)              # 上传 + 删除用同一个端点
        self.assertIn("?name=${encodeURIComponent(name)}&dir=", shared)
        self.assertIn("/files/download?path=", shared)
        self.assertIn("method: 'DELETE'", shared)
        self.assertEqual(self.client.get("/assets/composer-files.css").status_code, 200)

    def test_the_sidebar_previews_a_pane_after_hovering(self):
        """侧栏（会话树 + 最近）悬停 N 秒预览最后消息，和 home 卡片共用一份实现。"""
        self.login()
        page = self.console_surface()
        self.assertIn('console.css: pane-preview.css', page)
        self.assertIn("/assets/console.bundle.js?v=", page)
        self.assertIn("TmuxPanePreview", self.client.get("/assets/console.bundle.js").text)
        for marker in ("function setupPanePreview(){", "setupRecentPreview();",
                       "localStorage.getItem('tmux-preview-delay')",
                       "seconds*1000", "TmuxPanePreview.bind($('pane-list')",
                       "selector:'.recent-row',paneOf:element=>element.dataset.row",
                       "steadyDelay:true",
                       "new URLSearchParams(location.search).has('preview')",
                       "previewFromTranscript(data)"):
            self.assertIn(marker, page)
        # 不在 /api/home 的 pane 悬停时才取一次，60 秒内复用
        module = self.client.get("/assets/pane-preview.js").text
        for marker in ("const canHover = !!(window.matchMedia", "(hover: hover) and (pointer: fine)",
                       "function previewFor(paneId)", "function bind(container, config)",
                       "if (!canHover && !options.force) return;", "FETCH_TTL = 60_000",
                       "steadyDelay", "pendingPane",
                       "function previewFromTranscript(data)", "完整预览", "data-hp=\"full\""):
            self.assertIn(marker, module)
        css = self.client.get("/assets/pane-preview.css").text
        self.assertIn(".hover-preview{pointer-events:auto", css)
        self.assertIn(".hp-body.md table{", css)
        self.assertEqual(self.client.get("/assets/pane-preview.css").headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/assets/pane-preview.js").headers["cache-control"], "no-store")

    def test_versioned_assets_are_immutable_but_unversioned_keep_no_store(self):
        self.login()
        cases = {
            "/assets/theme.js": "no-store",
            "/assets/recents.js": "no-store",
            "/assets/pane-preview.css": "no-store",
            "/assets/xterm/xterm.js": None,
            "/assets/mermaid/mermaid.min.js": "public, max-age=604800",
        }
        for path, plain_header in cases.items():
            plain = self.client.get(path)
            self.assertEqual(plain.headers.get("cache-control"), plain_header, path)
            versioned = self.client.get(path + "?v=1")
            self.assertEqual(versioned.headers.get("cache-control"),
                             "public, max-age=31536000, immutable", path)

    def test_console_bundle_is_one_versioned_request(self):
        self.login()
        page = self.client.get("/").text
        self.assertIn("/assets/console.bundle.js?v=", page)
        self.assertNotIn('src="/assets/uploads.js"', page)
        self.assertNotIn('src="/assets/recents.js"', page)
        bundle = self.client.get("/assets/console.bundle.js")
        self.assertEqual(bundle.status_code, 200)
        self.assertIn("javascript", bundle.headers.get("content-type", ""))
        self.assertEqual(bundle.headers.get("cache-control"), "no-store")
        for marker in ("window.TmuxRecents", "window.TmuxUpload", "window.TmuxPaths",
                       "window.TmuxPrompt", "window.TmuxDownload", "window.TmuxTransfer",
                       "window.TmuxSettings", "window.TmuxPanePreview", "window.TmuxFiles",
                       "window.TmuxUploads", "window.TmuxMarkdownCjk", "FitAddon",
                       "console.bundle: vendor/xterm/xterm.js"):
            self.assertIn(marker, bundle.text)
        digest = hashlib.sha1(bundle.content).hexdigest()[:12]
        self.assertIn("/assets/console.bundle.js?v=" + digest, page)
        versioned = self.client.get("/assets/console.bundle.js?v=" + digest)
        self.assertEqual(versioned.headers.get("cache-control"),
                         "public, max-age=31536000, immutable")
        node = shutil.which("node")
        if node:
            with tempfile.NamedTemporaryFile("wb", suffix=".js", delete=False) as handle:
                handle.write(bundle.content)
                name = handle.name
            try:
                result = subprocess.run([node, "--check", name], capture_output=True,
                                        text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            finally:
                os.unlink(name)

    def test_console_app_is_one_versioned_request(self):
        """控制台应用脚本曾经是 index.html 里 156KB 的内联 module：每次打开
        控制台都要跟着 no-store 的页面重新下载。现在是版本化 asset，首访
        下载一次，之后走浏览器缓存；页面只剩 40KB。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn("/assets/console-app.js?v=", page)
        self.assertNotIn('<script type="module">', page)
        app = self.client.get("/assets/console-app.js")
        self.assertEqual(app.status_code, 200)
        self.assertIn("javascript", app.headers.get("content-type", ""))
        self.assertEqual(app.headers.get("cache-control"), "no-store")
        for marker in ("refreshTranscript(pane,{force:true})", "mergeIncrementalMessages(",
                       "function syncProgress(", "connectTranscript(id,true)"):
            self.assertIn(marker, app.text)
        digest = hashlib.sha1(app.content).hexdigest()[:12]
        self.assertIn("/assets/console-app.js?v=" + digest, page)
        versioned = self.client.get("/assets/console-app.js?v=" + digest)
        self.assertEqual(versioned.headers.get("cache-control"),
                         "public, max-age=31536000, immutable")
        node = shutil.which("node")
        if node:
            with tempfile.NamedTemporaryFile("wb", suffix=".mjs", delete=False) as handle:
                handle.write(app.content)
                name = handle.name
            try:
                result = subprocess.run([node, "--check", name], capture_output=True,
                                        text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            finally:
                os.unlink(name)

    def test_console_css_is_one_versioned_bundle(self):
        """控制台样式曾经是 <head> 里 42KB 的内联 <style> 加 8 个独立链接：
        每次打开控制台都要重新下载，冷启动多 8 次往返。现在打成一个版本化
        bundle，首访一次下载，之后走缓存；theme.css 仍是独立文件（全站共用，
        且必须在控制台规则之后加载）。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn("/assets/console.css?v=", page)
        self.assertNotIn('href="/assets/recents.css', page)
        self.assertNotIn('href="/assets/pane-preview.css', page)
        self.assertIn('href="/assets/theme.css?v=1"', page)
        css = self.client.get("/assets/console.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers.get("content-type", ""))
        self.assertEqual(css.headers.get("cache-control"), "no-store")
        for marker in ("console.css: console-base.css", "console.css: recents.css",
                       "console.css: path-links.css", "console.css: composer-files.css",
                       "console.css: downloads.css", "console.css: video-cards.css",
                       "console.css: upload-cards.css", "console.css: image-viewer.css",
                       "console.css: pane-preview.css",
                       ".assistant-body>.msg-actions{margin-top:12px;",
                       ".fullscreen-btn span{display:none}",
                       ".share-dialog{width:min(calc(100vw - 24px)"):
            self.assertIn(marker, css.text)
        digest = hashlib.sha1(css.content).hexdigest()[:12]
        self.assertIn("/assets/console.css?v=" + digest, page)
        versioned = self.client.get("/assets/console.css?v=" + digest)
        self.assertEqual(versioned.headers.get("cache-control"),
                         "public, max-age=31536000, immutable")

    def test_the_hover_preview_delay_is_a_follow_me_preference(self):
        """设置面板里可调（关闭 / 1 / 2 / 3 秒），存到账号；手机没有悬停，默认不绑。"""
        self.login()
        settings = self.client.get("/assets/settings.js").text
        for marker in ('id="ts-preview-delay"', '<option value="off">关闭</option>',
                       '<option value="1">1 秒</option>', "loadPreviewPref", "savePreviewDelay",
                       "localStorage.setItem(PREVIEW_KEY, next)"):
            self.assertIn(marker, settings)
        # 用独立的内存 home.db：这组断言会写偏好，不能落到部署里的 home.db
        client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                       session_secret="test-secret", terminal_factory=self.terminals,
                                       share_db=":memory:", queue_db=":memory:", home_db=":memory:",
                                       remote_store=self.remote_store, remote_runner_factory=FakeSSHRunner))
        self.assertEqual(client.post("/login", data={"password": "unit-test-admin-password"},
                                     follow_redirects=False).status_code, 303)
        self.assertEqual(client.put("/api/prefs", json={"preview": "2"}).status_code, 200)
        self.assertEqual(client.put("/api/prefs", json={"preview": "off"}).status_code, 200)
        self.assertEqual(client.put("/api/prefs", json={"preview": "9"}).status_code, 400)
        self.assertEqual(client.get("/api/prefs").json()["prefs"]["preview"], "off")

    def test_file_panel_can_rename_and_delete(self):
        """文件面板的删除/重命名：目录内改名、不覆盖同名、非空目录要显式递归、护栏挡住家目录。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            Path(directory, "notes.md").write_text("hi")
            Path(directory, "other.md").write_text("x")
            Path(directory, "empty").mkdir()
            Path(directory, "full").mkdir()
            Path(directory, "full", "inner.txt").write_text("deep")
            endpoint = "/api/panes/%254/files/rename"

            renamed = self.client.post(endpoint, json={"path": f"{directory}/notes.md", "name": "readme.md"})
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertEqual(renamed.json()["path"], f"{directory}/readme.md")
            self.assertTrue(Path(directory, "readme.md").exists())

            clash = self.client.post(endpoint, json={"path": f"{directory}/readme.md", "name": "other.md"})
            self.assertEqual(clash.status_code, 409)
            self.assertTrue(Path(directory, "other.md").exists())
            self.assertTrue(Path(directory, "readme.md").exists())

            traversal = self.client.post(endpoint, json={"path": f"{directory}/readme.md", "name": "../evil"})
            self.assertEqual(traversal.status_code, 422)
            missing = self.client.post(endpoint, json={"path": f"{directory}/nope.md", "name": "x.md"})
            self.assertEqual(missing.status_code, 409)

            delete = "/api/panes/%254/files/delete"
            self.assertEqual(self.client.post(delete, json={"paths": [f"{directory}/readme.md"]}).status_code, 200)
            self.assertFalse(Path(directory, "readme.md").exists())
            self.assertEqual(self.client.post(delete, json={"paths": [f"{directory}/empty"]}).status_code, 200)
            self.assertFalse(Path(directory, "empty").exists())

            refused = self.client.post(delete, json={"paths": [f"{directory}/full"]})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("递归", refused.json()["detail"])
            forced = self.client.post(delete, json={"paths": [f"{directory}/full"], "recursive": True})
            self.assertEqual(forced.status_code, 200, forced.text)
            self.assertFalse(Path(directory, "full").exists())

            # 护栏：家目录 / 顶层目录不许动
            self.assertEqual(self.client.post(delete, json={"paths": [os.path.expanduser("~")]}).status_code, 422)
            self.assertEqual(self.client.post(delete, json={"paths": ["/tmp"]}).status_code, 422)
            self.assertEqual(self.client.post(delete, json={"paths": []}).status_code, 422)
            guest = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           remote_store=self.remote_store, home_db=":memory:"))
            self.assertEqual(guest.post(endpoint, json={"path": "/tmp/x", "name": "y"}).status_code, 401)
            self.assertEqual(guest.post(delete, json={"paths": ["/tmp/x"]}).status_code, 401)

    def test_file_panel_exposes_rename_and_delete_controls(self):
        """面板里的入口：选中栏按钮、右键/长按菜单、F2 与 Delete 键。"""
        self.login()
        page = self.console_surface()
        for marker in ('id="file-rename"', 'id="file-delete"', 'id="file-dialog"', 'id="file-menu"',
                       'id="fd-input"', 'id="fd-recursive"', "fileDialog(options)", "function renameFile(path)",
                       "async function deleteFiles(paths)", "function setupFileMenu()",
                       "/files/rename`", "/files/delete`", "contextmenu", "event.pointerType==='mouse'",
                       "520)", "event.key==='F2'", "symbol id=\"i-trash\""):
            self.assertIn(marker, page)

    def test_chat_paths_can_be_looked_up_before_opening(self):
        """聊天气泡里的路径：先问服务器这是目录还是文件，再决定打开面板/下载/预览。"""
        self.login()
        with tempfile.TemporaryDirectory() as directory:
            self.tmux.panes[0]["path"] = directory
            Path(directory, "notes.md").write_text("# hi")
            Path(directory, "sub").mkdir()
            for path, kind, exists in [(f"{directory}/sub", "dir", True),
                                       (f"{directory}/notes.md", "file", True),
                                       (f"{directory}/nope.txt", None, False)]:
                response = self.client.get("/api/panes/%254/files/stat", params={"path": path})
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["kind"], kind, path)
                self.assertEqual(body["exists"], exists, path)
            # ~ 交给服务端展开，相对路径按 pane 的 cwd 解析
            self.assertEqual(self.client.get("/api/panes/%254/files/stat",
                                             params={"path": "notes.md"}).json()["name"], "notes.md")
            self.assertEqual(self.client.get("/api/panes/%254/files/stat",
                                             params={"path": "~/definitely-missing"}).json()["exists"], False)
            self.assertEqual(self.client.get("/api/panes/%254/files/stat", params={"path": ""}).status_code, 422)

    def test_relative_paths_fall_back_to_the_parent_directory(self):
        """agent 常吐“自己 cwd 下的相对路径”，按 cwd 找不到就往上退一级再试。"""
        self.login()
        with tempfile.TemporaryDirectory() as root:
            work = Path(root, "work")
            work.mkdir()
            Path(root, "ate", "s3k").mkdir(parents=True)
            Path(root, "ate", "s3k", "crossboard.md").write_text("# x")
            Path(work, "ate").mkdir()
            Path(work, "ate", "local.md").write_text("# y")
            self.tmux.panes[0]["path"] = str(work)
            endpoint = "/api/panes/%254/files/stat"
            # cwd 命中：offset 0
            local = self.client.get(endpoint, params={"path": "ate/local.md"}).json()
            self.assertTrue(local["exists"])
            self.assertEqual(local["offset"], 0)
            self.assertEqual(local["path"], str(work / "ate" / "local.md"))
            # cwd 里没有 → 往上一级（work 的父目录）命中
            up = self.client.get(endpoint, params={"path": "ate/s3k/crossboard.md"}).json()
            self.assertTrue(up["exists"], up)
            self.assertEqual(up["offset"], 1)
            self.assertEqual(up["path"], str(Path(root, "ate", "s3k", "crossboard.md")))
            # 哪儿都没有 → 仍然是不存在
            missing = self.client.get(endpoint, params={"path": "ate/s3k/nope.md"}).json()
            self.assertFalse(missing["exists"])

    def test_a_path_one_level_deeper_is_found_by_searching(self):
        """agent 报 ate/s3k/x.md，真实位置在 cwd 的下一级 runs/195/ate/s3k/ 里（#6 那种）。"""
        self.login()
        with tempfile.TemporaryDirectory() as root:
            work = Path(root, "work")
            deep = work / "runs" / "195" / "ate" / "s3k"
            deep.mkdir(parents=True)
            (deep / "retrain195_1103.md").write_text("# 报告")
            (deep / "evidence").mkdir()
            self.tmux.panes[0]["path"] = str(work)
            endpoint = "/api/panes/%254/files/stat"
            hit = self.client.get(endpoint, params={"path": "ate/s3k/retrain195_1103.md"}).json()
            self.assertTrue(hit["exists"], hit)
            self.assertEqual(hit["via"], "search")
            self.assertEqual(hit["kind"], "file")
            self.assertEqual(hit["path"], str(deep / "retrain195_1103.md"))
            self.assertEqual(hit["relative"], "runs/195/ate/s3k/retrain195_1103.md")
            # 目录同样能找到
            folder = self.client.get(endpoint, params={"path": "ate/s3k/evidence"}).json()
            self.assertEqual(folder["kind"], "dir")
            self.assertEqual(folder["via"], "search")
            # 真没有的东西仍然判空，且带 via=none
            miss = self.client.get(endpoint, params={"path": "ate/s3k/definitely-not-here.md"}).json()
            self.assertFalse(miss["exists"])
            self.assertEqual(miss["via"], "none")
            # 前端会把相对路径拼成「旧 cwd + 相对路径」的绝对路径发过来，同样要能搜到
            joined = self.client.get(endpoint, params={"path": str(work / "ate/s3k/retrain195_1103.md")}).json()
            self.assertTrue(joined["exists"], joined)
            self.assertEqual(joined["via"], "search")
            self.assertEqual(joined["path"], str(deep / "retrain195_1103.md"))
            # 气泡里要说明是“在子目录找到的”，别让人以为原样就存在
            script = self.client.get("/assets/path-links.js").text
            self.assertIn("info.via === 'search' && info.relative", script)
            self.assertIn("子目录 · ${info.relative}", script)

    def test_mobile_downloads_ask_before_opening(self):
        """移动端下载必须先确认：iOS 直接打开下载链接会跳进没有返回按钮的预览。"""
        self.login()
        page = self.console_surface()
        self.assertIn('console.css: downloads.css', page)
        self.assertIn('/assets/console.bundle.js?v=', page)
        self.assertIn("TmuxDownload", self.client.get("/assets/console.bundle.js").text)
        self.assertIn("TmuxDownload.maybe(", page)
        self.assertIn('downloadButton.onclick=event=>{', page)

        module = self.client.get("/assets/downloads.js").text
        self.assertIn("const MOBILE = /Android|iPhone|iPad|iPod", module)
        self.assertIn("function maybe(href, options)", module)
        self.assertIn("if (!MOBILE) { go(href); return Promise.resolve(true); }", module)
        self.assertIn("window.TmuxDownload = { isMobile, maybe, ask, go };", module)
        self.assertIn(self.client.get("/assets/downloads.css").status_code, (200,))

        preview = self.client.get("/preview?pane=%25254").text
        self.assertIn('/assets/downloads.js', preview)
        self.assertIn("TmuxDownload.maybe(href,{name,meta:path});", preview)
        # 路径气泡里的下载也走确认
        self.assertIn("window.TmuxDownload.maybe(href, {name})",
                      self.client.get("/assets/path-links.js").text)

    def test_clicking_an_uploaded_image_opens_the_viewer(self):
        """手机上点缩略图不能跳到裸图片页（那看着就是白屏）：要走应用内查看层。"""
        self.login()
        page = self.console_surface()
        self.assertIn('console.css: image-viewer.css', page)
        module = self.client.get("/assets/image-viewer.js").text
        self.assertEqual(self.client.get("/assets/image-viewer.css").status_code, 200)
        for marker in ("TmuxImageViewer", "data-iview-close", "a.upload-thumb[data-upload]",
                       "history.pushState", "popstate"):
            self.assertIn(marker, module)
        # 文件已经不在了就别跳过去白屏
        self.assertIn("broken", module)
        # 预览页（/preview）也要能点开
        preview = self.client.get("/preview?pane=%251").text
        self.assertIn('/assets/image-viewer.js', preview)

    def test_chat_messages_turn_paths_into_links(self):
        """独立的路径后处理：/assets/path-links.js，控制台和 /preview 都用它。"""
        self.login()
        page = self.console_surface()
        self.assertIn('console.css: path-links.css', page)
        self.assertIn('console.bundle: assets/path-links.js',
                      self.client.get("/assets/console.bundle.js").text)
        self.assertIn("function enhancePaths(root){", page)
        # 懒处理：渲染时不扫，碰到哪条消息才处理哪条（长会话别每次全量扫文本）
        self.assertIn("function enhancePathsLazy(container, force){", page)
        # 重渲染（轮询刷新换掉 DOM）之后要再跑一遍，否则新节点永远没被处理
        self.assertIn("enhancePathsLazy(host,true)", page)
        # .plink 建好之后才轮到视频卡片
        self.assertIn("enhanceVideos(root)", page)
        self.assertIn("container.dataset.pathsBound='1';", page)
        self.assertIn("'pointerover',touch,true", page)
        self.assertNotIn("rewriteLocalLinks(host);enhancePaths(host);", page)
        self.assertIn("onDir:path=>{openFiles(path);}", page)
        self.assertIn("onMd:path=>openMarkdownViewer(", page)
        self.assertIn('id="md-download"', page)

        preview = self.client.get("/preview?pane=%251").text
        self.assertIn('/assets/path-links.js', preview)
        self.assertIn("function enhancePaths(root){", preview)

        module = self.client.get("/assets/path-links.js").text
        self.assertIn("function activateAt(x, y, scope)", module)
        # 代码块里"整块就是一条路径"也要能点开/下载（以前 pre 整块被跳过）
        # 代码块按行看（以前"整块只有一条路径"才认，#179 那种"路径 + 说明"就漏了）
        self.assertIn("function enhanceCodeLines(", module)
        self.assertIn("enhanceCodeLines(root, pane, options, cwd)", module)
        self.assertIn("__plinkOpen", module)
        # 悬停气泡：文件名 + 大小 + 预览/下载；log/txt 这类点开是预览而不是直接下载
        self.assertIn("TEXT_EXT", module)
        self.assertIn("plink-card-actions", module)
        self.assertIn('data-plink-act="download"', module)
        self.assertIn("downloadHref", module)
        self.assertIn("onPreview:path=>openMarkdownViewer(", page)
        self.assertIn(".plink-card{position:fixed", self.client.get("/assets/path-links.css").text)
        for marker in ("window.TmuxPaths = {enhance, rawHref, lookup, activateAt, downloadHref, hideCard, bareCodePath};",
                       "files/stat?path=", "onDir", "onMd", "onMissing", "dataset.plinkPath"):
            self.assertIn(marker, module)
        # 中文标点也是边界：`本地文件：/Users/...` 这类纯文本必须能识别
        self.assertIn("：:，、；;！？!?", module)
        # 只有一段的（/grid、/OUT-OF-BAND）和接口前缀（/api、/assets）不当文件链接
        self.assertIn("function plausible(path)", module)
        self.assertIn("WEB_PREFIX", module)
        self.assertEqual(self.client.get("/assets/path-links.css").status_code, 200)

    def test_the_embedded_preview_has_the_same_composer(self):
        """多窗口里那一格就是 /preview，不能没有附件（否则行为分叉）。"""
        self.login()
        preview = self.client.get("/preview?pane=%251").text
        self.assertIn('/assets/composer-files.css', preview)
        self.assertIn('/assets/composer-files.js', preview)
        self.assertIn('<div class="cf-bar">', preview)
        self.assertIn('<div class="cf-strip" id="uploads" hidden></div>', preview)
        self.assertIn("window.TmuxFiles.mount({", preview)
        self.assertIn("const prompt=files?files.prompt():'';", preview)
        # 标题也统一走最近/收藏里的名字
        self.assertIn("async function applyCustomName(){", preview)

    def test_upload_limit_returns_413(self):
        with patch.dict("os.environ", {"TMUX_WEB_FILE_UPLOAD_MAX": "8"}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", queue_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            client.post("/login", data={"password": "unit-test-admin-password"})
            with tempfile.TemporaryDirectory() as directory:
                self.tmux.panes[0]["path"] = directory
                response = client.put(f"/api/panes/%254/files/upload?dir={directory}&name=big.bin",
                                      content=b"0123456789")
        self.assertEqual(response.status_code, 413)

    def test_remote_claude_pane_can_be_shared_now(self):
        host_key = "10.0.0.8 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDoNcEyBJB6EHylGUOrjANaF/n45q3A0l+Kg3QGMCbo2"
        fingerprint = host_key_fingerprint(host_key)
        self.login()
        host_id = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.8", "port": 22, "username": "ubuntu",
            "auth_type": "password", "credential": "secret", "host_key": host_key,
            "fingerprint": fingerprint, "confirm_fingerprint": fingerprint,
        }).json()["id"]
        created = self.client.post("/api/shares", json={
            "pane_id": f"{host_id}~%9", "slug": "remote-share", "password": "", "expires_in": 3600,
        })
        self.assertEqual(created.status_code, 201)
        guest = TestClient(self.client.app)
        self.assertEqual(guest.get("/share/remote-share/api/transcript").status_code, 200)


class PaneValidationDedupTests(unittest.TestCase):
    """A pane the route already validated must not pay for a second listing."""

    LIST_LINE_14 = "<::>".join([
        "%1", "sess:0.0", "sess", "0", "shell", "0", "zsh", "title",
        "/work", "1", "200", "50", "1234567", "4242",
    ])
    LIST_LINE_13 = "<::>".join([
        "%1", "sess:0.0", "sess", "0", "shell", "0", "zsh", "title",
        "/work", "1", "200", "50", "1234567",
    ])

    def _backend(self, list_output):
        calls = []

        def fake_run(*args, input_bytes=None):
            calls.append(args)
            if args[0] == "list-panes":
                return list_output
            if args[0] == "capture-pane":
                return "screen\n"
            if args[0] == "display-message":
                return "4242\n"
            raise AssertionError("unexpected tmux call: %r" % (args,))

        backend = TmuxBackend()
        backend._run = fake_run
        return backend, calls

    def test_list_panes_parses_pane_pid_and_keeps_old_shape(self):
        backend, _ = self._backend(self.LIST_LINE_14)
        self.assertEqual(backend.list_panes()[0]["pane_pid"], "4242")
        backend, _ = self._backend(self.LIST_LINE_13)
        self.assertNotIn("pane_pid", backend.list_panes()[0])

    def test_capture_with_validated_pane_skips_the_listing(self):
        backend, calls = self._backend(self.LIST_LINE_14)
        backend.capture("%1", 500)
        self.assertEqual([c[0] for c in calls], ["list-panes", "capture-pane"])
        calls.clear()
        backend.capture("%1", 500, pane={"id": "%1", "command": "zsh"})
        self.assertEqual([c[0] for c in calls], ["capture-pane"])

    def test_capture_many_with_listed_panes_skips_validation(self):
        two = self.LIST_LINE_14 + "\n" + self.LIST_LINE_14.replace("%1", "%2")
        backend, calls = self._backend(two)
        panes = {p["id"]: p for p in backend.list_panes()}
        calls.clear()
        out = backend.capture_many(["%1", "%2"], 40, panes=panes)
        self.assertEqual(sorted(out), ["%1", "%2"])
        self.assertEqual([c[0] for c in calls], ["capture-pane", "capture-pane"])
        calls.clear()
        backend.capture_many(["%1", "%2"], 40)
        self.assertEqual([c[0] for c in calls],
                         ["list-panes", "capture-pane", "list-panes", "capture-pane"])

    def test_transcript_skips_display_message_when_pid_known(self):
        backend, calls = self._backend(self.LIST_LINE_14)
        patches = (
            mock.patch.object(tmux_web.transcript_reader, "read_transcript",
                              return_value={"messages": []}),
            mock.patch.object(tmux_web.transcript_reader, "provider_for_command",
                              return_value="claude"),
            mock.patch.object(tmux_web.transcript_reader, "resume_session_hint",
                              return_value=None),
        )
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        backend.transcript("%1", 4, pane={"id": "%1", "command": "claude",
                                          "path": "/work", "title": "t", "pane_pid": "4242"})
        self.assertEqual(calls, [])
        calls.clear()
        # the validating path lists once, and the listing itself now carries
        # pane_pid, so it needs no extra display-message call either
class WaitForPasteVisibleTests(unittest.TestCase):
    """Press Enter only once the pasted text is actually showing in the prompt.

    The old code guessed with a length-based delay; a big paste can still be
    landing when the first C-m fires, and the key is dropped.  Polling the
    screen for our own fingerprint closes that gap.
    """

    class Backend(tmux_web.TmuxBackend):
        def __init__(self, screens):
            self.screens = list(screens)
            self.sent = []

        def _run(self, *args, **kwargs):
            self.sent.append(args)
            if args and args[0] == "capture-pane":
                return self.screens.pop(0) if self.screens else ""
            return ""

    def captures(self, backend):
        return [call for call in backend.sent if call[:1] == ("capture-pane",)]

    def test_returns_true_once_the_text_shows_up(self):
        backend = self.Backend(["line one\n", "❯ hello world\n"])
        with patch("tmux_web.time.sleep"):
            self.assertTrue(backend.wait_for_paste_visible("%1", "hello world"))
        self.assertEqual(len(self.captures(backend)), 2)

    def test_returns_false_when_the_text_never_shows_up(self):
        backend = self.Backend([""])
        with patch("tmux_web.time.sleep"):
            self.assertFalse(backend.wait_for_paste_visible("%1", "hello world"))
        self.assertEqual(len(self.captures(backend)), 20)

    def test_empty_fingerprint_returns_false_without_polling(self):
        backend = self.Backend(["❯ \n"])
        with patch("tmux_web.time.sleep"):
            self.assertFalse(backend.wait_for_paste_visible("%1", "   "))
        self.assertEqual(self.captures(backend), [])


class StillInPromptTests(unittest.TestCase):
    """Only the last 3 lines say "still in the prompt".

    After a submit the message stays visible in the transcript just above
    the input box; a wider window would keep "finding" our text after a
    successful submit and report a false not-submitted.
    """

    class Backend(tmux_web.TmuxBackend):
        def __init__(self, screen):
            self.screen = screen

        def _run(self, *args, **kwargs):
            return self.screen

    def test_text_on_the_third_to_last_line_is_still_in_the_prompt(self):
        backend = self.Backend("top\n❯ hello world\n─\n❯ \n")
        self.assertTrue(backend.still_in_prompt("%1", "hello world"))

    def test_text_only_in_the_transcript_is_not_in_the_prompt(self):
        backend = self.Backend("❯ hello world\ntop\n─\n❯ \n")
        self.assertFalse(backend.still_in_prompt("%1", "hello world"))

    def test_text_on_the_last_line_is_still_in_the_prompt(self):
        backend = self.Backend("top\n❯ hello world\n")
        self.assertTrue(backend.still_in_prompt("%1", "hello world"))


class ResendEnterTests(unittest.TestCase):
    """send() resends Enter until the text leaves the prompt, and says so when it cannot."""

    class Backend(tmux_web.TmuxBackend):
        def __init__(self, screens):
            self.screens = list(screens)
            self.sent = []

        def _run(self, *args, **kwargs):
            self.sent.append(args)
            if args and args[0] == "capture-pane":
                return self.screens.pop(0) if self.screens else ""
            return ""

    def presses(self, backend):
        return [call for call in backend.sent
                if call[:2] == ("send-keys", "-t") and call[-1] == "C-m"]

    def run_send(self, backend, text="hello world"):
        backend.validate_pane = lambda pane_id: {"id": pane_id, "command": "claude"}
        with patch("tmux_web.time.sleep"):
            return backend.send("%4", text, True)

    def test_sends_once_when_the_paste_shows_up_and_submits(self):
        backend = self.Backend(["❯ hello world\n", ""])
        self.assertTrue(self.run_send(backend))
        self.assertEqual(len(self.presses(backend)), 1)

    def test_submitted_message_left_in_the_transcript_is_not_still_in_the_prompt(self):
        # The message stays visible above the input box after a submit; only
        # the prompt itself is empty.  That must count as submitted, not as
        # a stuck paste.
        backend = self.Backend(["❯ hello world\n", "hello world\nx\n─\n❯ \n",
                                "hello world\nx\n─\n❯ \n"])
        self.assertTrue(self.run_send(backend))
        self.assertEqual(len(self.presses(backend)), 1)

    def test_presses_again_when_the_first_enter_is_swallowed(self):
        backend = self.Backend(["❯ hello world\n", "❯ hello world\n", ""])
        self.assertTrue(self.run_send(backend))
        self.assertEqual(len(self.presses(backend)), 2)

    def test_returns_false_and_warns_after_five_unanswered_enters(self):
        backend = self.Backend(["❯ hello world\n"] + ["❯ hello world\n"] * 30)
        with self.assertLogs("tmux_web", level="WARNING") as logs:
            self.assertFalse(self.run_send(backend))
        self.assertEqual(len(self.presses(backend)), 5)
        self.assertTrue(any("not submitted" in line and "%4" in line for line in logs.output))

    def test_no_enter_and_true_when_submit_is_false(self):
        backend = self.Backend([])
        backend.validate_pane = lambda pane_id: {"id": pane_id, "command": "claude"}
        with patch("tmux_web.time.sleep"):
            self.assertTrue(backend.send("%4", "hello world", False))
        self.assertEqual(self.presses(backend), [])


class FingerprintWindowTests(unittest.TestCase):
    """A multi-line prompt counts as still in the prompt even when its tail spans two lines.

    Regression for the 2026-11-11 incident: a ~250-char message on oracle
    claude %0 sat unsubmitted in the input box while send() reported success.
    #11 had narrowed the check to the last 3 lines; the fingerprint (last 40
    chars) straddled the last two input lines, and the bottom 3 rows hold only
    the last input line plus the divider and the status line, so the check
    could not assemble the fingerprint and called it submitted after one Enter.
    """

    TEXT = (
        "vendor 問題 , 简单说是整个8533数模混合时钟通路一路缩小duty "
        "cycle直到最后不work。修的方式也是整个通路上找合适的多个PMOS和NMOS，"
        "用analog方式删一点finger，每个MOS修一点点来调整duty。累计达到效果。\n\n"
        "所以問題處在 duty cycle 到後面快被擠占沒了, "
        "需要我們基於這個信息 做正向的行爲模型建模 驗證 以及 walkaround 問題 , 目標是 7500 跑穩定"
    )

    # The screen as captured on the incident pane: transcript, divider, the
    # multi-line input box, divider, status line.  The fingerprint (last 40
    # chars) spans the last two input lines.
    SCREEN = (
        "● Compacted while idle, before the prompt cache expired\n"
        "● +4 more tasks completed\n"
        "────────────────────────────────────────────────────────────────────\n"
        "❯ vendor 問題 ,  简单说是整个8533数模混合时钟通路一路缩小duty\n"
        "  cycle直到最后不work。修的方式也是整个通路上找合适的多个PMOS和NMO\n"
        "  S，用analog方式删一点finger，每个MOS修一点点来调整duty。累计达到\n"
        "  效果。\n"
        "\n"
        "  所以問題處在 duty cycle  到後面快被擠占沒了,\n"
        "  需要我們基於這個信息 做正向的行爲模型建模  驗證 以及 walkaround\n"
        "  問題 , 目標是 7500 跑穩定\n"
        "────────────────────────────────────────────────────────────────────\n"
        "  ⏵⏵ auto mode on · 3 shells · ⧉ 6\n"
    )

    class Backend(tmux_web.TmuxBackend):
        def __init__(self, screens):
            self.screens = list(screens)
            self.sent = []

        def _run(self, *args, **kwargs):
            self.sent.append(args)
            if args and args[0] == "capture-pane":
                return self.screens.pop(0) if self.screens else ""
            return ""

    def presses(self, backend):
        return [call for call in backend.sent
                if call[:2] == ("send-keys", "-t") and call[-1] == "C-m"]

    def run_send(self, backend, text):
        backend.validate_pane = lambda pane_id: {"id": pane_id, "command": "claude"}
        with patch("tmux_web.time.sleep"):
            return backend.send("%4", text, True)

    def test_kimi_framed_editor_excludes_history_and_joins_wrapped_text(self):
        body = "fixture message spanning two rows"
        frame = "╭──────╮\n│ > fixture message span │\n│ ning two rows │\n╰──────╯\nNORMAL\nctrl-x help"
        self.assertTrue(self.Backend([frame]).still_in_prompt("%1", body))
        cleared = "❯ " + body + "\n╭──────╮\n│ >  │\n╰──────╯\nNORMAL\nctrl-x help"
        self.assertFalse(self.Backend([cleared]).still_in_prompt("%1", body))

    def test_hermes_history_cannot_confirm_the_next_question_with_a_retry(self):
        prompt = "❯ hello world"
        frame = "──────── workspace ──────\n{}\n────────────\n ☤ Hermes · idle"
        waiting = prompt + "\n" + frame.format("选择 provider？\n  1. openai")
        backend = self.Backend([frame.format(prompt), waiting, waiting])
        self.assertTrue(self.run_send(backend, "hello world"))
        self.assertEqual(len(self.presses(backend)), 1)
        self.assertFalse(self.Backend([waiting]).fingerprint_in_prompt("%1", "hello world"))

    def test_multiline_prompt_tail_is_still_in_the_prompt(self):
        backend = self.Backend([self.SCREEN])
        self.assertTrue(backend.still_in_prompt("%1", self.TEXT))

    def test_send_presses_five_times_and_warns_when_the_multiline_prompt_never_leaves(self):
        backend = self.Backend([self.SCREEN] * 8)
        with self.assertLogs("tmux_web", level="WARNING") as logs:
            self.assertFalse(self.run_send(backend, self.TEXT))
        self.assertEqual(len(self.presses(backend)), 5)
        self.assertTrue(any("not submitted" in line and "%4" in line for line in logs.output))

    def test_mid_word_wrap_does_not_hide_the_fingerprint(self):
        # The screen wraps "walkaround" as "walkar" / "ound": joining the
        # lines gains a space the text never had, so plain substring matching
        # misses the fingerprint even when the whole input box is in view.
        screen = self.SCREEN.replace("驗證 以及 walkaround\n  問題",
                                     "驗證 以及 walkar\n  ound 問題")
        backend = self.Backend([screen])
        self.assertTrue(backend.still_in_prompt("%1", self.TEXT))


class SendSubmittedTests(unittest.TestCase):
    """POST /input (and the relay) must report retrying instead of a silent sent.

    The in-request send still fails the same way, but the failure is now
    handed to the background retry loop: the response says "retrying" and
    the client watches the transcript poll for the outcome.  The old
    pasted_not_submitted wording survives as the loop's terminal state
    (cap reached or pane gone), covered by BackgroundResendRouteTests.
    """

    class NeverSubmits(FakeTmux):
        def send(self, pane_id, text, submit=True, pane=None):
            self.sent.append((pane_id, text, submit))
            return False if submit else True

    class AlwaysSubmits(FakeTmux):
        def send(self, pane_id, text, submit=True, pane=None):
            self.sent.append((pane_id, text, submit))
            return True

    def client_for(self, tmux):
        return TestClient(create_app(
            tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:", queue_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))

    def login(self, client):
        response = client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_input_reports_retrying_with_200(self):
        client = self.client_for(self.NeverSubmits())
        self.login(client)
        response = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "retrying")
        self.assertFalse(response.json()["submitted"])
        self.assertTrue(response.json()["retrying"])

    def test_input_reports_sent_when_submitted(self):
        client = self.client_for(self.AlwaysSubmits())
        self.login(client)
        response = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        self.assertTrue(response.json()["submitted"])

    def test_input_without_submit_still_reports_sent(self):
        client = self.client_for(self.NeverSubmits())
        self.login(client)
        response = client.post("/api/panes/%254/input", json={"text": "hello", "submit": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")

    def test_relay_message_reports_retrying(self):
        client = self.client_for(self.NeverSubmits())
        self.login(client)
        response = client.post("/api/relay/message", json={"target": "%4", "text": "请先跑测试"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "retrying")
        self.assertFalse(response.json()["submitted"])
        self.assertTrue(response.json()["retrying"])

    def guest_for_share(self, tmux, slug="share001"):
        tmux.panes[0]["command"] = "claude"
        client = self.client_for(tmux)
        self.login(client)
        created = client.post("/api/shares", json={
            "pane_id": "%4", "slug": slug, "password": "", "expires_in": 3600,
        })
        self.assertEqual(created.status_code, 201)
        return TestClient(client.app), slug

    def test_share_input_reports_retrying_with_200(self):
        guest, slug = self.guest_for_share(self.NeverSubmits())
        response = guest.post(f"/share/{slug}/api/input", json={"text": "继续分析", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "retrying")
        self.assertFalse(response.json()["submitted"])
        self.assertTrue(response.json()["retrying"])

    def test_share_input_reports_sent_when_submitted(self):
        guest, slug = self.guest_for_share(self.AlwaysSubmits())
        response = guest.post(f"/share/{slug}/api/input", json={"text": "继续分析", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        self.assertTrue(response.json()["submitted"])

    def test_share_input_without_submit_still_reports_sent(self):
        guest, slug = self.guest_for_share(self.NeverSubmits())
        response = guest.post(f"/share/{slug}/api/input", json={"text": "继续分析", "submit": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")

    def test_share_input_none_return_treated_as_submitted(self):
        guest, slug = self.guest_for_share(FakeTmux())
        response = guest.post(f"/share/{slug}/api/input", json={"text": "继续分析", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        self.assertTrue(response.json()["submitted"])


class FingerprintInPromptTests(unittest.TestCase):
    """fingerprint_in_prompt answers the same question still_in_prompt asks,
    but for a stored fingerprint instead of the full text."""

    class Backend(tmux_web.TmuxBackend):
        def __init__(self, screen):
            self.screen = screen

        def _run(self, *args, **kwargs):
            return self.screen

    def test_fingerprint_in_the_prompt_region_is_found(self):
        backend = self.Backend("top\n❯ hello world\n─\n❯ \n")
        self.assertTrue(backend.fingerprint_in_prompt("%1", "hello world"))

    def test_fingerprint_only_in_the_transcript_is_not_found(self):
        backend = self.Backend("❯ hello world\ntop\n─\n❯ \n")
        self.assertFalse(backend.fingerprint_in_prompt("%1", "hello world"))

    def test_fingerprint_matches_across_a_mid_word_wrap(self):
        # The screen wraps "walkaround" as "walkar" / "ound": the
        # whitespace-stripped match still finds it.
        backend = self.Backend("top\n❯ walkar\nound 問題\n")
        self.assertTrue(backend.fingerprint_in_prompt("%1", "walkaround 問題"))


class BackgroundResendTests(unittest.IsolatedAsyncioTestCase):
    """The background loop presses Enter until the fingerprint leaves the prompt."""

    class Transport:
        def __init__(self, visible):
            self.visible = list(visible)
            self.keys = []

        def fingerprint_in_prompt(self, pane_id, fingerprint):
            value = self.visible.pop(0) if self.visible else False
            if isinstance(value, Exception):
                raise value
            return value

        def send_key(self, pane_id, key, pane=None):
            self.keys.append((pane_id, key))

    def manager_for(self, transport, **kwargs):
        params = {"interval": 0.01, "time_limit": 30.0}
        params.update(kwargs)
        manager = tmux_web.BackgroundResend(**params)
        manager.bind(lambda pane_id: (transport, pane_id))
        return manager

    async def wait_for(self, manager, pane_id, wanted, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = manager.status(pane_id)
            if record and record["status"] in wanted:
                return record
            await asyncio.sleep(0.01)
        return manager.status(pane_id)

    async def test_presses_until_the_fingerprint_leaves_then_reports_submitted(self):
        transport = self.Transport([True, True, False])
        manager = self.manager_for(transport)
        manager.start("%4", "hello world")
        record = await self.wait_for(manager, "%4", ("submitted",))
        self.assertEqual(record["status"], "submitted")
        self.assertEqual(record["presses"], 2)
        self.assertEqual(transport.keys, [("%4", "Enter")] * 2)

    async def test_stops_at_the_cap_and_warns(self):
        transport = self.Transport([True] * 100)
        manager = self.manager_for(transport, time_limit=0.05)
        with self.assertLogs("tmux_web", level="WARNING") as logs:
            manager.start("%4", "hello world")
            record = await self.wait_for(manager, "%4", ("failed",))
        self.assertEqual(record["status"], "failed")
        self.assertGreaterEqual(record["presses"], 1)
        self.assertTrue(any("gave up" in line and "%4" in line for line in logs.output))

    async def test_pane_gone_stops_without_more_presses(self):
        transport = self.Transport([True, TmuxError("can't find pane: %4")])
        manager = self.manager_for(transport)
        manager.start("%4", "hello world")
        record = await self.wait_for(manager, "%4", ("failed",))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["presses"], 1)
        await asyncio.sleep(0.05)
        self.assertEqual(len(transport.keys), 1)

    async def test_transient_error_skips_a_press_but_keeps_going(self):
        transport = self.Transport([RuntimeError("ssh timed out"),
                                    RuntimeError("ssh timed out"), False])
        manager = self.manager_for(transport)
        manager.start("%4", "hello world")
        record = await self.wait_for(manager, "%4", ("submitted",))
        self.assertEqual(record["status"], "submitted")
        self.assertEqual(record["presses"], 0)

    async def test_new_start_supersedes_the_old_task(self):
        first = self.Transport([True] * 100)
        second = self.Transport([True] * 100)
        manager = self.manager_for(first)
        manager.start("%4", "first message")
        await asyncio.sleep(0.05)
        self.assertGreater(len(first.keys), 0)
        manager.bind(lambda pane_id: (second, pane_id))
        manager.start("%4", "second message")
        first_keys = len(first.keys)
        await asyncio.sleep(0.05)
        self.assertEqual(len(first.keys), first_keys, "旧任务被顶掉后不能再按回车")
        self.assertGreater(len(second.keys), 0, "新任务继续重试")
        self.assertEqual(manager.status("%4")["status"], "retrying")
        manager.cancel("%4", "superseded")

    async def test_cancel_marks_the_record_superseded_and_stops_pressing(self):
        transport = self.Transport([True] * 100)
        manager = self.manager_for(transport)
        manager.start("%4", "hello world")
        await asyncio.sleep(0.03)
        record = manager.cancel("%4", "superseded")
        self.assertEqual(record["status"], "superseded")
        keys = len(transport.keys)
        await asyncio.sleep(0.05)
        self.assertEqual(len(transport.keys), keys)
        self.assertEqual(manager.status("%4")["status"], "superseded")

    async def test_cancel_unknown_pane_is_a_noop(self):
        manager = self.manager_for(self.Transport([]))
        self.assertIsNone(manager.cancel("%9", "superseded"))
        self.assertIsNone(manager.status("%9"))

    async def test_status_hides_the_fingerprint(self):
        transport = self.Transport([True, False])
        manager = self.manager_for(transport)
        manager.start("%4", "secret text here")
        record = manager.status("%4")
        self.assertEqual(record["status"], "retrying")
        self.assertNotIn("fingerprint", record)
        self.assertNotIn("secret text here", str(record))

    async def test_transport_without_a_fingerprint_check_stops_cleanly(self):
        class NoCheck:
            def send_key(self, pane_id, key, pane=None):
                raise AssertionError("must not press without a check")

        manager = self.manager_for(NoCheck())
        manager.start("%4", "hello world")
        record = await self.wait_for(manager, "%4", ("failed",))
        self.assertEqual(record["status"], "failed")


class BackgroundResendRouteTests(unittest.TestCase):
    """POST /input hands a stuck paste to the background loop instead of failing it."""

    class ScriptedTmux(FakeTmux):
        """Fake app backend whose screen is scripted and whose send() is the real one."""

        def __init__(self, screens):
            super().__init__()
            self.screens = list(screens)
            self.tmux_calls = []

        def _run(self, *args, **kwargs):
            self.tmux_calls.append(args)
            if args and args[0] == "capture-pane":
                return self.screens.pop(0) if self.screens else ""
            return ""

        def send(self, pane_id, text, submit=True, pane=None):
            return tmux_web.TmuxBackend.send(self, pane_id, text, submit, pane=pane)

    def client_for(self, tmux, manager=None):
        return TestClient(create_app(
            tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:", queue_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
            resend_manager=manager,
        ))

    def login(self, client):
        response = client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def poll_resend(self, client, pane_id="%254", timeout=5.0):
        deadline = time.monotonic() + timeout
        resend = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/panes/{pane_id}/transcript")
            self.assertEqual(response.status_code, 200)
            resend = response.json().get("resend")
            if resend and resend["status"] != "retrying":
                return resend
            time.sleep(0.02)
        return resend

    def test_input_returns_retrying_and_the_background_loop_submits(self):
        # In-request send burns 6 stuck screens (wait 1 + press checks 4 +
        # final 1); the background loop then presses 6 more and the 7th check
        # sees the empty screen.
        tmux = self.ScriptedTmux(["❯ hello\n"] * 12 + [""] * 10)
        tmux.panes[0]["command"] = "claude"
        manager = tmux_web.BackgroundResend(interval=0.02, time_limit=30.0)
        # The client must stay open as a context manager: starlette 1.10
        # gives each bare request its own portal and cancels background
        # tasks when the response body is read, so only a shared portal
        # lets the retry loop run between requests.  (Production uvicorn
        # keeps one loop, so this is test-only.)
        with self.client_for(tmux, manager) as client:
            self.login(client)
            with patch("tmux_web.time.sleep"):
                response = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "retrying")
            self.assertFalse(response.json()["submitted"])
            self.assertTrue(response.json()["retrying"])
            resend = client.get("/api/panes/%254/transcript").json().get("resend")
            self.assertEqual(resend["status"], "retrying")
            final = self.poll_resend(client)
            self.assertEqual(final["status"], "submitted")
            self.assertEqual(final["presses"], 6)
            self.assertEqual(tmux.keys, [("%4", "Enter")] * 6)
            self.assertEqual(len([call for call in tmux.tmux_calls
                                  if call[:2] == ("send-keys", "-t") and call[-1] == "C-m"]), 5)
            data = client.get("/api/panes/%254/transcript").json()
            self.assertEqual(len(data["messages"]), 2)

    def test_input_cap_fails_back_to_pasted_not_submitted(self):
        tmux = self.ScriptedTmux(["❯ hello\n"] * 100)
        tmux.panes[0]["command"] = "claude"
        manager = tmux_web.BackgroundResend(interval=0.02, time_limit=0.3)
        with self.client_for(tmux, manager) as client:
            self.login(client)
            with patch("tmux_web.time.sleep"):
                response = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
            self.assertEqual(response.json()["status"], "retrying")
            with self.assertLogs("tmux_web", level="WARNING") as logs:
                final = self.poll_resend(client)
            self.assertEqual(final["status"], "failed")
            self.assertGreaterEqual(final["presses"], 1)
            self.assertTrue(any("gave up" in line and "%4" in line for line in logs.output))

    def test_new_send_supersedes_the_old_retry(self):
        tmux = self.ScriptedTmux(["❯ hello\n"] * 10 + [""] * 60)
        tmux.panes[0]["command"] = "claude"
        manager = tmux_web.BackgroundResend(interval=0.02, time_limit=30.0)
        with self.client_for(tmux, manager) as client:
            self.login(client)
            with patch("tmux_web.time.sleep"):
                first = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
                self.assertEqual(first.json()["status"], "retrying")
                second = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
            self.assertEqual(second.json()["status"], "sent")
            self.assertTrue(second.json()["submitted"])
            resend = client.get("/api/panes/%254/transcript").json().get("resend")
            self.assertEqual(resend["status"], "superseded")
            time.sleep(0.1)
            keys = len(tmux.keys)
            time.sleep(0.3)
            self.assertEqual(len(tmux.keys), keys, "旧重试被顶掉后不能再按回车")

    def test_relay_message_returns_retrying(self):
        tmux = self.ScriptedTmux(["❯ 请先跑测试\n"] * 20)
        manager = tmux_web.BackgroundResend(interval=0.02, time_limit=0.3)
        client = self.client_for(tmux, manager)
        self.login(client)
        with patch("tmux_web.time.sleep"):
            response = client.post("/api/relay/message", json={"target": "%4", "text": "请先跑测试"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "retrying")
        self.assertFalse(response.json()["submitted"])
        self.assertTrue(response.json()["retrying"])

    def guest_for_share(self, tmux, manager, slug="share001"):
        tmux.panes[0]["command"] = "claude"
        client = self.client_for(tmux, manager)
        self.login(client)
        created = client.post("/api/shares", json={
            "pane_id": "%4", "slug": slug, "password": "", "expires_in": 3600,
        })
        self.assertEqual(created.status_code, 201)
        return TestClient(client.app), slug

    def test_share_input_returns_retrying(self):
        tmux = self.ScriptedTmux(["❯ 继续分析\n"] * 20)
        manager = tmux_web.BackgroundResend(interval=0.02, time_limit=0.3)
        guest, slug = self.guest_for_share(tmux, manager)
        with patch("tmux_web.time.sleep"):
            response = guest.post(f"/share/{slug}/api/input", json={"text": "继续分析", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "retrying")
        self.assertFalse(response.json()["submitted"])
        self.assertTrue(response.json()["retrying"])
        transcript = guest.get(f"/share/{slug}/api/transcript")
        self.assertEqual(transcript.status_code, 200)
        self.assertEqual(transcript.json().get("resend", {}).get("status"), "retrying")


if __name__ == "__main__":
    unittest.main()


class GitWorkspaceTests(unittest.TestCase):
    """检查点 / 回滚 / worktree：在临时仓库里真跑一遍 git，不打桩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()

        def git(*args):
            return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.repo / "app.txt").write_text("one\n")
        git("add", "app.txt")
        git("commit", "-q", "-m", "init")

        self.tmux = FakeTmux()
        self.tmux.panes[0]["path"] = str(self.repo)
        self.client = TestClient(create_app(
            self.tmux, password="unit-test-admin-password", session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner))
        self.login()

    def login(self):
        response = self.client.post("/login", data={"password": "unit-test-admin-password"},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_it_reports_branch_changes_and_worktrees(self):
        (self.repo / "app.txt").write_text("dirty\n")
        payload = self.client.get("/api/panes/%254/git").json()
        self.assertTrue(payload["repo"])
        self.assertEqual(payload["branch"], "main")
        self.assertFalse(payload["clean"])
        self.assertEqual([item["path"] for item in payload["changes"]], ["app.txt"])
        self.assertEqual([item["branch"] for item in payload["worktrees"]], ["main"])

    def test_a_directory_that_is_not_a_repo_says_so(self):
        self.tmux.panes[0]["path"] = self.tmp.name
        self.assertFalse(self.client.get("/api/panes/%254/git").json()["repo"])

    def test_checkpoint_then_restore_puts_the_working_tree_back(self):
        (self.repo / "app.txt").write_text("checkpoint state\n")
        created = self.client.post("/api/panes/%254/git/checkpoint",
                                   json={"label": "before the agent went wild"}).json()
        self.assertTrue(created["created"], created)
        ref = created["ref"]
        self.assertTrue(ref.startswith("refs/tmuxweb/checkpoints/"))

        listed = self.client.get("/api/panes/%254/git").json()["checkpoints"]
        self.assertEqual([item["ref"] for item in listed], [ref])

        (self.repo / "app.txt").write_text("the agent broke it\n")
        self.assertTrue(self.client.get("/api/panes/%254/git").json()["changes"])

        restored = self.client.post("/api/panes/%254/git/restore", json={"ref": ref}).json()
        self.assertTrue(restored["matches"], f"工作区必须和检查点一致（不是和 HEAD 一致）：{restored}")
        self.assertEqual((self.repo / "app.txt").read_text(), "checkpoint state\n")
        # 分支没被动过：检查点只是 ref，不是提交到 main 上
        status = subprocess.run(["git", "log", "--oneline", "-1"], cwd=self.repo,
                                capture_output=True, text=True).stdout
        self.assertIn("init", status)

    def test_a_clean_tree_creates_nothing(self):
        payload = self.client.post("/api/panes/%254/git/checkpoint", json={"label": "x"}).json()
        self.assertFalse(payload["created"])
        self.assertIn("干净", payload["detail"])

    def test_untracked_files_survive_a_restore(self):
        (self.repo / "app.txt").write_text("state\n")
        ref = self.client.post("/api/panes/%254/git/checkpoint", json={}).json()["ref"]
        (self.repo / "notes.txt").write_text("mine\n")
        payload = self.client.post("/api/panes/%254/git/restore", json={"ref": ref}).json()
        self.assertTrue((self.repo / "notes.txt").exists(), "回滚不能删掉检查点之后新增的文件")
        self.assertIn("notes.txt", [item["path"] for item in payload["untracked"]])

    def test_diff_shows_the_pending_change(self):
        (self.repo / "app.txt").write_text("changed\n")
        payload = self.client.get("/api/panes/%254/git/diff").json()
        self.assertIn("app.txt", payload["stat"])
        self.assertIn("+changed", payload["patch"])

    def test_commit_with_push_and_pr_stops_where_it_breaks(self):
        """一键提交：提交成功、推送失败时，得能分清两件事。"""
        (self.repo / "app.txt").write_text("one\ncompose me\n")
        payload = self.client.post("/api/panes/%254/git/commit",
                                   json={"message": "改 app.txt", "push": True}).json()
        self.assertTrue(payload["committed"], payload)
        self.assertEqual(len(payload["sha"]), 7)
        self.assertFalse(payload["pushed"], "没有远端，推不上去")
        log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.repo,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(log, "改 app.txt")

    def test_a_clean_tree_cannot_be_committed(self):
        payload = self.client.post("/api/panes/%254/git/commit", json={"message": "空的"}).json()
        self.assertFalse(payload["committed"], payload)
        self.assertEqual(payload["exit"], 11)

    def test_branch_info_and_a_suggested_message(self):
        (self.repo / "app.txt").write_text("changed\n")
        branch = self.client.get("/api/panes/%254/git/branch").json()
        self.assertEqual(branch["branch"], "main")
        self.assertFalse(branch["gh"], "这台机器上 gh 没装或没登录")
        status = self.client.get("/api/panes/%254/git").json()
        self.assertIn("app.txt", status["suggested_message"])

    def test_diff_can_be_limited_to_one_file(self):
        (self.repo / "other.txt").write_text("new file\n")
        subprocess.run(["git", "add", "other.txt"], cwd=self.repo, capture_output=True)
        (self.repo / "app.txt").write_text("changed\n")
        payload = self.client.get("/api/panes/%254/git/diff", params={"path": "app.txt"}).json()
        self.assertIn("app.txt", payload["patch"])
        self.assertNotIn("other.txt", payload["patch"])

    def test_worktree_gives_the_thread_its_own_checkout(self):
        payload = self.client.post("/api/panes/%254/git/worktree", json={"name": "task1"}).json()
        self.assertTrue(Path(payload["path"]).is_dir(), payload)
        self.assertEqual(payload["branch"], "wt/task1")
        trees = self.client.get("/api/panes/%254/git").json()["worktrees"]
        self.assertEqual(sorted(item["branch"] for item in trees), ["main", "wt/task1"])
