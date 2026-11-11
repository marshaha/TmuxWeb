import hashlib
import io
import os
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

    def capture_ansi(self, pane_id, history=500, pane=None):
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

    def add_host(self, **data):
        self._sequence += 1
        host = {"id": "0123456789abc%03d" % self._sequence, "enabled": True, "created_at": 0.0,
                "position": len(self.hosts), **data}
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
                    "jump_host_id"):
            if changes.get(key) is not None:
                updated[key] = changes[key]
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


class FakeSSHRunner:
    sent = []
    screen = "\x1b[32mREMOTE_OK\x1b[0m\n❯ "
    auto_reply = None

    def __init__(self, host):
        self.host = host
        self.created = []
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

    def kill_session(self, name):
        FakeSSHRunner.killed = name
        return {"session": name, "killed": True}

    def probe_agents(self, timeout=12):
        return [dict(item) for item in getattr(self, "agents", [])]
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
    def capture_ansi(self, pane_id, history=5000, pane=None): return FakeSSHRunner.screen
    def capture(self, pane_id, history=5000, pane=None, visible_only=False):
        return ANSI_RE.sub("", FakeSSHRunner.screen)
    def snapshot_ansi(self, pane_id, history=5000, known_digest=None, pane=None):
        content = self.capture_ansi(pane_id, history)
        digest = hashlib.sha1(content.encode()).hexdigest()
        return digest, (None if digest == known_digest else content)
    def transcript(self, pane_id, limit=200, refresh=False, pane=None):
        return {"pane": pane_id, "provider": "claude", "session_id": "remote-session-1", "mtime": 7,
                "messages": [{"id": "r1", "role": "user", "text": "远端问题", "timestamp": None}]}
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
            share_db=":memory:",
            remote_store=self.remote_store,
            remote_runner_factory=FakeSSHRunner,
        ))

    def login(self):
        response = self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

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
        )

    def _patch_addresses(self, serve="No serve config", json_dns="robo.neko-chroma.ts.net."):
        """直连地址端点靠外部命令取地址，测试里把 subprocess.run 换掉。"""
        from types import SimpleNamespace
        import subprocess

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
            first = self.client.get("/api/system/addresses").json()
        self.assertFalse([a for a in first["addresses"] if a["url"].startswith("https://")],
                         first["addresses"])

        patcher, _ = self._patch_addresses(serve="https://robo.neko-chroma.ts.net (tailnet only)\n")
        with patcher:
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
        page = self.client.get("/").text
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
        page = self.client.get("/").text
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

    def test_send_button_turns_into_a_stop_button_while_working(self):
        self.login()
        page = self.client.get("/").text
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

            def list_panes(self, timeout=15):
                raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=timeout)

        client = TestClient(create_app(
            self.tmux,
            password="unit-test-admin-password",
            session_secret="test-secret",
            terminal_factory=self.terminals,
            share_db=":memory:",
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
        self.assertEqual(hung["status"], "offline")
        self.assertIn("timed out", hung["error"])
        self.assertEqual(hung["panes"], [])

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
        page = self.client.get("/?pane=%254").text
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
        page = self.client.get("/?pane=%254").text
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
        page = self.client.get("/?pane=%254").text
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
        page = self.client.get("/").text
        self.assertIn("function isTouch(){return matchMedia('(pointer:coarse)').matches", page)
        self.assertIn("function canAutoFocusComposer(){return !isMobile()&&!isTouch();}", page)
        self.assertIn("if(state.mode==='chat'&&canAutoFocusComposer())input.focus();", page)
        self.assertIn("if(mode==='chat'&&state.selected&&canAutoFocusComposer())input.focus();", page)
        self.assertNotIn("if(state.mode==='chat'&&!isMobile())input.focus();", page)

    def test_fullscreen_follows_the_visual_viewport_on_a_phone(self):
        """手机上全屏写死 100vh，键盘一弹出输入框就被顶到键盘底下（只露出上半截）。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn(".main:fullscreen,.main.fullscreen-fallback{width:100vw;height:var(--visual-height,100dvh);", page)
        self.assertIn("max-height:var(--visual-height,100dvh)}", page)
        self.assertIn("padding-bottom:calc(9px + env(safe-area-inset-bottom,0px) + 4px)}", page)

    def test_labelled_buttons_cannot_be_squeezed_into_overflow(self):
        """带字的按钮一律 flex:0 0 auto + nowrap：窄屏时被挤扁，文字就会顶出边框（对话全屏 那次）。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn(".share-btn,.fullscreen-btn{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;white-space:nowrap}", page)
        # 手机上仍然是图标按钮（文字藏起来），不会挤
        self.assertIn(".fullscreen-btn{width:34px;padding:0;justify-content:center}", page)
        self.assertIn(".fullscreen-btn span{display:none}", page)

    def test_a_block_copy_button_shows_its_label(self):
        """块顶栏的「复制 / 源码」按钮：图标没有宽高时 svg 是 300×150，会把文字顶出按钮。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn(".msg-copy .ico,.block-btn .ico{width:11px;height:11px;", page)
        self.assertIn(".msg-copy,.block-btn{display:inline-flex;align-items:center}", page)
        # 文字单独一层：复制成功只换字，不会把图标删掉
        self.assertEqual(page.count('<span class="btn-label">复制全部</span>'), 2)
        self.assertIn('<span class="btn-label">复制</span>', page)
        self.assertIn("const label=button.querySelector('.btn-label')||button;", page)
        self.assertIn("label.textContent=ok?'已复制':'复制失败';", page)

    def test_a_wide_table_cannot_push_the_copy_row_off_screen(self):
        """手机上宽表格：表格在自己的框里横滚，气泡宽度被夹住，复制行留在气泡里且不会被挤出去。"""
        self.login()
        page = self.client.get("/").text
        # 复制行仍在气泡里（两个构造消息的地方都是这个结构）
        self.assertEqual(page.count('${markdown(message.text)}${copy}'), 1)
        self.assertEqual(page.count('${markdown(message.text)}${copyButton(message)}'), 1)
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
        page = self.client.get("/").text
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
        self.assertEqual(session.pane_id, "%4")
        self.assertEqual(session.inputs, [b"hello\r"])
        self.assertEqual(session.resizes, [(120, 40)])
        # 关闭发生在服务端任务里，客户端断开后要给事件循环一点时间（CI 上更慢）
        deadline = time.time() + 2.0
        while time.time() < deadline and not session.closed:
            time.sleep(0.02)
        self.assertTrue(session.closed)

    def test_index_is_served(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Tmux Chat", response.text)
        self.assertIn("pane-list", response.text)
        self.assertIn("composer-card", response.text)
        self.assertIn("terminal-mode", response.text)
        self.assertIn("capture-output", response.text)
        self.assertNotIn(".terminal{display:block;min-width:max-content", response.text)
        self.assertIn("connectTranscript(id,true)", response.text)
        self.assertIn("ansi_to_html", response.text)
        self.assertIn("renderTableLine", response.text)
        self.assertIn("terminalWidth", response.text)
        self.assertIn(".terminal-cell.wide{width:2ch}", response.text)
        self.assertIn(".terminal-table-line{display:inline;white-space:nowrap}", response.text)
        self.assertNotIn(".terminal-table-line{display:block;height:1.5em", response.text)
        self.assertIn("visibilitychange", response.text)
        self.assertIn("connectTranscript", response.text)
        self.assertIn("marked.parse", response.text)
        self.assertIn('id="fullscreen"', response.text)
        self.assertIn('<main class="main" id="main">', response.text)
        self.assertIn("requestFullscreen", response.text)
        self.assertIn('id="sidebar-close"', response.text)
        self.assertIn('id="drawer-backdrop"', response.text)
        self.assertIn("tmux-sidebar-collapsed", response.text)
        self.assertIn("env(safe-area-inset-bottom)", response.text)
        self.assertIn("visualViewport", response.text)
        self.assertIn("mobile-composing", response.text)
        self.assertIn("setComposing", response.text)
        self.assertIn("state.mode==='chat'&&canAutoFocusComposer()", response.text)
        self.assertIn("mode==='chat'&&state.selected&&canAutoFocusComposer()", response.text)
        self.assertIn("viewport-fit=cover", response.text)
        self.assertIn('rel="manifest"', response.text)
        self.assertIn("apple-mobile-web-app-capable", response.text)
        self.assertIn("serviceWorker.register('/sw.js')", response.text)
        self.assertIn("-webkit-text-size-adjust:100%", response.text)
        self.assertIn(".chat-message.assistant{justify-content:flex-start}", response.text)
        self.assertIn("background:var(--card);color:var(--ink);font-size:15px", response.text)
        self.assertIn("refreshTranscript(id,{force=false,refresh=false}", response.text)
        self.assertIn("state.selected!==id||state.mode!=='chat'", response.text)
        self.assertIn("/api/hosts'", response.text)
        for marker in ('id="live-toggle"', "CAPTURE_PAINT_MS", "CAPTURE_MAX_LINES", "pushTerminal",
                       "TERM_BACKLOG_MAX", "queueCapture", "pauseLive", "resumeLive"):
            self.assertIn(marker, response.text)
        self.assertIn("treeDetails", response.text)
        self.assertIn("treeOpen:new Set()", response.text)
        self.assertIn("tmux-tree-open", response.text)
        self.assertIn("state.treeOpen.has(key)?' open':''", response.text)
        self.assertIn("tree-host", response.text)
        self.assertIn("e.isComposing||isMobile()", response.text)
        self.assertIn('id="terminal-keys"', response.text)
        self.assertIn('data-key="ArrowUp"', response.text)
        self.assertIn("scheduleTerminalFit", response.text)
        self.assertIn("visualViewport.addEventListener('resize',handleVisualViewportChange)", response.text)
        self.assertIn("pendingMessages:new Map()", response.text)
        self.assertIn("addOptimisticMessage", response.text)
        self.assertIn("const baseline=waiting[0][1].baselineIds;", response.text)
        self.assertIn("const fresh=messages.filter(message=>message.role==='user'&&!baseline.has(String(message.id)))", response.text)
        self.assertIn("messageTextKey(message.text)===key", response.text)
        self.assertIn("function pendingAnchorIndex(messages,pending)", response.text)
        self.assertIn("pendingAnchorIndex(messages,item.pending)", response.text)
        self.assertIn("a rewritten/expanded echo still belongs to the same send", response.text)
        self.assertIn("发送中", response.text)
        self.assertIn("已发送 ✓", response.text)
        self.assertIn("发送失败", response.text)
        self.assertIn("pending-wave", response.text)
        self.assertIn("pane-state", response.text)
        self.assertIn("refreshPaneStates", response.text)
        self.assertIn("/api/panes/status", response.text)
        self.assertIn("applyPaneStates", response.text)
        self.assertIn("p.capabilities&&p.capabilities.share", response.text)
        self.assertIn('id="file-panel"', response.text)
        self.assertIn('id="files-toggle"', response.text)
        self.assertIn('id="file-input"', response.text)
        self.assertIn("setupFiles", response.text)
        self.assertIn("openFiles", response.text)
        self.assertIn("uploadFiles", response.text)
        self.assertIn("/files/download?path=", response.text)
        self.assertIn("dataTransfer.files", response.text)
        self.assertIn(".file-panel[hidden],.pane-list[hidden]{display:none!important}", response.text)
        self.assertIn('id="file-close"', response.text)
        self.assertIn("tmux-files-mode", response.text)
        self.assertIn("transcriptSignature", response.text)
        self.assertIn("signature!==state.transcriptSig", response.text)

        # the UI uses one inline SVG sprite instead of ad-hoc text glyphs and emoji
        for symbol in ("i-folder", "i-server", "i-share", "i-external", "i-refresh", "i-chevron-left",
                       "i-menu", "i-upload", "i-close", "i-file", "i-link", "i-parent"):
            self.assertIn(f'<symbol id="{symbol}"', response.text, symbol)
        for glyph in ("▤", "⌁", "⇧", "↻", "☰", "‹", "⤒", "✕", "⌘", "↰", "⛶", "📁", "📄", "🔗"):
            self.assertNotIn(glyph, response.text, glyph)
        self.assertIn(".ico{", response.text)
        self.assertIn('aria-label="文件"', response.text)

        # the file panel is scoped to one pane: switching pane/host drops the old
        # listing instead of reloading the previous path on the new machine
        self.assertIn("filesPane:null", response.text)
        self.assertIn("state.filesPane&&state.filesPane!==id", response.text)
        self.assertIn("state.filesPane===state.selected?state.filesPath:null", response.text)
        # 目录列表请求带序号：后发的赢，快速切目录不会被慢响应覆盖
        self.assertIn("pane!==state.selected||seq!==state.filesSeq)return", response.text)
        # the path bar names the machine the listing came from
        self.assertIn('id="file-host"', response.text)

        # creating a tmux session on a host that has none
        self.assertIn('id="session-dialog"', response.text)
        self.assertIn('data-new-session="', response.text)
        self.assertIn('<symbol id="i-plus"', response.text)
        self.assertIn("async function submitSessionForm(", response.text)
        self.assertIn("api('/api/sessions'", response.text)
        # 关闭窗格：顶栏入口 + 二次确认 + 打 DELETE
        self.assertIn('id="pane-close"', response.text)
        self.assertIn('id="pane-close-dialog"', response.text)
        self.assertIn("关闭这个窗格？", response.text)
        self.assertIn("`/api/panes/${encodeURIComponent(id)}`,{method:'DELETE'}", response.text)
        self.assertIn("e.stopPropagation();state.sessionHost=add.dataset.newSession", response.text)

        # per-message copy + per-block controls (code / mermaid / table)
        self.assertIn('data-msg-copy=', response.text)

        # switching panes must reuse the cached transcript instead of reloading
        self.assertIn("function paintCachedTranscript(", response.text)
        self.assertIn("function rememberTranscript(", response.text)
        self.assertIn("function savePaneUi(", response.text)
        self.assertIn("transcripts:new Map()", response.text)
        self.assertIn("drafts:new Map()", response.text)
        self.assertIn("更新中…", response.text)
        self.assertIn('复制全部', response.text)
        self.assertIn("async function copyText(", response.text)
        self.assertIn("function enhanceBlocks(", response.text)
        self.assertIn("data-block-copy", response.text)
        self.assertIn("data-block-source", response.text)
        self.assertIn("/assets/mermaid/mermaid.min.js", response.text)
        mermaid = self.client.get("/assets/mermaid/mermaid.min.js")
        self.assertEqual(mermaid.status_code, 200)
        self.assertGreater(len(mermaid.content), 100000)
        self.assertEqual(self.client.get("/assets/mermaid/other.js").status_code, 404)
        self.assertIn("$('file-host').textContent=owner?owner.name", response.text)

        # editable path bar + multi-select zip download
        self.assertIn('id="file-path" type="text"', response.text)
        self.assertIn("$('file-path').addEventListener('keydown'", response.text)
        self.assertIn("$('file-path').addEventListener('focus'", response.text)
        # selection follows desktop conventions instead of a mode toggle
        self.assertNotIn('id="file-select"', response.text)
        self.assertNotIn("filesSelect", response.text)
        self.assertIn("e.ctrlKey||e.metaKey", response.text)
        self.assertIn("e.shiftKey", response.text)
        self.assertIn("function selectRange(", response.text)
        self.assertIn("function togglePick(", response.text)
        self.assertIn('class="file-check"', response.text.replace("${", ""))
        self.assertIn('<symbol id="i-select"', response.text)
        self.assertIn("async function downloadArchive(", response.text)
        # the toolbar "up" button was redundant with the ".." row; it is a batch
        # download button now, and both zip entry points share one code path
        self.assertNotIn('id="file-up"', response.text)
        self.assertIn('id="file-download"', response.text)
        self.assertIn('<symbol id="i-download"', response.text)
        self.assertIn("$('file-download').onclick", response.text)
        # definition + footer button + toolbar button (two call sites) + 右键/长按菜单
        self.assertEqual(response.text.count("downloadArchive("), 5)
        self.assertIn("/files/zip", response.text)
        self.assertIn("function syncFileSelection()", response.text)

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

    def test_ansi_sequences_can_be_removed_without_changing_columns(self):
        raw = "| A   | B |\n\x1b[38;5;231m| 123 | x |\x1b[0m"
        self.assertEqual(ANSI_RE.sub("", raw), "| A   | B |\n| 123 | x |")

    def test_panes_expose_provider_and_capabilities(self):
        self.login()
        pane = self.client.get("/api/panes").json()["panes"][0]
        self.assertEqual(pane["provider"], "codex")
        self.assertEqual(pane["capabilities"], {"transcript": True, "terminal": True,
                                                "relay": True, "share": True})
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
                                           share_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            self.assertEqual(client.get("/api/panes").status_code, 401)
            self.assertEqual(client.get("/api/panes", headers={"X-TmuxWeb-Token": "wrong"}).status_code, 401)
            self.assertEqual(client.get("/api/panes",
                                        headers={"X-TmuxWeb-Token": "relay-token-value"}).status_code, 200)
            self.assertEqual(client.get("/api/panes", headers={
                "Authorization": "Bearer relay-token-value"}).status_code, 200)

    def test_the_sidebar_always_opens_on_the_session_list(self):
        """The file panel is an action, not a remembered preference: it used to be
        restored from localStorage, so a chat opened from the board or the grid could
        start with 文件管理 instead of the server list."""
        self.login()
        page = self.client.get("/").text
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
                                           share_db=":memory:", remote_store=FakeRemoteStore(),
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

    def test_every_pane_gets_a_stable_short_number(self):
        """多服务器场景：每个窗口一个 #N，和主机/pane id 无关，编号不回收。"""
        # 独立的 home.db：编号是全局的，不能和别的用例共用一张表
        self.client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                            session_secret="test-secret",
                                            terminal_factory=self.terminals,
                                            share_db=":memory:", home_db=":memory:",
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
        page = self.client.get("/").text
        self.assertIn('class="session-num" id="selected-number"', page)
        self.assertIn("$('selected-number')", page)
        self.assertIn("num.textContent=p.number?`#${p.number}`", page)
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
        self.assertIn("const shownTitle=customName||p.title||p.target;", page)
        self.assertIn("document.title=p.number?`#${p.number} · ${shownTitle} · Tmux Chat`", page)

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

        page = self.client.get("/").text
        self.assertIn('class="pane-num"', page)
        self.assertIn("/^#?\\d{1,5}$/.test(wanted.trim())", page)
        self.assertIn("state.panes.find(p=>p.number===number)", page)
        self.assertIn("${p.number?`#${p.number}`:'—'}", page)
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
                                               share_db=":memory:", remote_store=FakeRemoteStore(),
                                               remote_runner_factory=FakeSSHRunner))
                response = client.post("/login", data={"password": "unit-test-admin-password"}, headers=headers,
                                       follow_redirects=False)
                self.assertIn("secure", response.headers["set-cookie"].lower())
            # untrusted proxy: the forwarded protocol is ignored
            with patch.dict("os.environ", {"TMUX_WEB_TRUST_PROXY": "", "TMUX_WEB_SECURE_COOKIES": "auto"}):
                client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                               session_secret="test-secret", terminal_factory=self.terminals,
                                               share_db=":memory:", remote_store=FakeRemoteStore(),
                                               remote_runner_factory=FakeSSHRunner))
                response = client.post("/login", data={"password": "unit-test-admin-password"}, headers=headers,
                                       follow_redirects=False)
                self.assertNotIn("secure", response.headers["set-cookie"].lower())

    def test_allowed_hosts_can_be_pinned(self):
        with patch.dict("os.environ", {"TMUX_WEB_ALLOWED_HOSTS": "chat.example.com"}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", remote_store=FakeRemoteStore(),
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
        self.assertEqual(init.json()["result"]["serverInfo"]["name"], "tmuxweb-relay")

        listing = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
        self.assertEqual({tool["name"] for tool in listing["result"]["tools"]},
                         {"list_agents", "ask_agent", "send_agent", "run_command",
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
                                           share_db=":memory:", remote_store=FakeRemoteStore(),
                                           remote_runner_factory=FakeSSHRunner))
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            open_mcp = client.post("/mcp", json=body)
            # the rest of the API keeps its token/cookie check
            still_protected = client.get("/api/hosts")
        self.assertEqual(open_mcp.status_code, 200)
        self.assertEqual(len(open_mcp.json()["result"]["tools"]), 14)
        self.assertEqual(still_protected.status_code, 401)

    def test_mcp_endpoint_requires_the_api_token(self):
        anonymous = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                         session_secret="test-secret", terminal_factory=self.terminals,
                                         share_db=":memory:", remote_store=FakeRemoteStore(),
                                         remote_runner_factory=FakeSSHRunner))
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        self.assertEqual(anonymous.post("/mcp", json=body).status_code, 401)
        token = "unit-test-api-token"
        with patch.dict("os.environ", {"TMUX_WEB_API_TOKEN": token}):
            client = TestClient(create_app(self.tmux, password="unit-test-admin-password",
                                           session_secret="test-secret", terminal_factory=self.terminals,
                                           share_db=":memory:", remote_store=FakeRemoteStore(),
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
        self.assertEqual(self.client.post("/api/sessions",
                                          json={"name": "x1", "agent": "nope"}).status_code, 422)
        with mock.patch.object(tmux_web.agent_catalog, "probe_local", return_value=[]):
            response = self.client.post("/api/sessions", json={"name": "x2", "agent": "claude"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("not installed", response.json()["detail"])
        # 引号没闭合的 args 不能被偷偷吞掉
        self.assertEqual(self.client.post("/api/sessions",
                                          json={"name": "x3", "agent": "claude",
                                                "agent_args": '--x "unclosed'}).status_code, 422)

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

    def test_the_prompt_card_asset_and_markup_are_wired(self):
        script = self.client.get("/assets/prompt-card.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("TmuxPrompt", script.text)
        self.assertIn("/prompt/choose", script.text)
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
        for marker in ('id="prompt-card"', 'id="prompt-card-options"', 'id="prompt-card-esc"',
                       '/assets/console.bundle.js?v=', "TmuxPrompt.start(()=>state.selected)"):
            self.assertIn(marker, html, marker)
        self.assertIn("TmuxPrompt", self.client.get("/assets/console.bundle.js").text)
        self.assertIn(".prompt-card{", html)
        # 卡片本身不滚动，只有选项列表滚：标题/开关/Esc 必须常驻可见
        self.assertIn(".prompt-options{display:flex;flex-direction:column;gap:6px;margin-top:8px;min-height:0;flex:1 1 auto", html)
        self.assertIn("id=\"prompt-card-toggles\"", html)

    def test_the_prompt_endpoint_needs_a_login(self):
        for path in ("/api/panes/%254/prompt",):
            response = self.client.get(path, follow_redirects=False)
            self.assertIn(response.status_code, (303, 401, 403), path)
        response = self.client.post("/api/panes/%254/prompt/choose", json={"index": 0},
                                    follow_redirects=False)
        self.assertIn(response.status_code, (303, 401, 403))

    def test_a_menu_on_screen_becomes_buttons(self):
        """kimi 的信任对话框：聊天里要能看见并点，而不是只能切终端。"""
        self.enable_prompt_card()
        self.tmux.output = (
            "──────────────────────────────\n"
            "  Trust this folder?\n"
            "  ↑↓ navigate · Enter select · Esc exit\n"
            "\n"
            "  /tmp\n"
            "\n"
            "   ❯ Trust and continue\n"
            "     Exit\n"
            " ──────────────────────────────\n"
        )
        payload = self.client.get("/api/panes/%254/prompt").json()
        prompt = payload["prompt"]
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["title"], "Trust this folder?")
        self.assertEqual([option["label"] for option in prompt["options"]],
                         ["Trust and continue", "Exit"])
        self.assertEqual(payload["screen"], payload["screen"])
        self.assertTrue(payload["screen"], "要带屏幕原文，用户得能核对在答什么")

    def test_no_menu_means_no_buttons(self):
        self.login()
        self.tmux.output = "hello\n❯ Ask Codex to do anything\n"
        payload = self.client.get("/api/panes/%254/prompt").json()
        self.assertIsNone(payload["prompt"])
        self.assertEqual(payload["screen"], [])

    def test_choosing_sends_keys_and_recheck_the_screen(self):
        self.enable_prompt_card()
        menu = ("  Select a model  (type to search)\n"
                "  ↑↓ navigate · Enter select · Esc cancel\n\n"
                "     Alpha\n"
                "  ❯ Beta\n"
                "     Gamma\n")
        self.tmux.output = menu
        sent_keys = []

        def recording(pane_id, key, pane=None):
            sent_keys.append(key)
            if key == "Enter":
                self.tmux.output = "  done\n"      # 选完之后菜单消失

        self.tmux.send_key = recording
        response = self.client.post("/api/panes/%254/prompt/choose", json={"index": 2})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["pending"], "发完立刻返回，屏幕由前端轮询")
        self.assertEqual(sent_keys[-1], "Enter")
        self.assertEqual(sent_keys.count("Down"), 2, "第三项要往下走两步")
        self.assertGreaterEqual(sent_keys.count("Up"), 3, "先顶到第一项")
        # 复核交给下一次 /prompt（屏幕已经变了）
        self.assertIsNone(self.client.get("/api/panes/%254/prompt").json()["prompt"])

    def test_a_numbered_menu_can_be_chosen_by_digit(self):
        self.enable_prompt_card()
        self.tmux.output = (" Context is getting full.\n\n"
                            " What would you like to do?\n"
                            " 1. Compact and continue\n"
                            " 2. Start a new session\n"
                            " Enter select · Esc cancel\n")
        sent_keys = []

        def recording(pane_id, key, pane=None):
            sent_keys.append(key)
            if key == "Enter":
                self.tmux.output = "compacting…\n"

        self.tmux.send_key = recording
        result = self.client.post("/api/panes/%254/prompt/choose", json={"key": "2"}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(sent_keys, ["2", "Enter"])
        self.assertIsNone(self.client.get("/api/panes/%254/prompt").json()["prompt"])

    def test_a_switch_can_be_flipped_from_the_card(self):
        """kimi 的 Thinking 开关：点 Max 就发一次 →，然后复核屏幕真的变了。"""
        self.enable_prompt_card()
        self.tmux.output = ("  Select a model  (type to search)\n"
                            "  ↑↓ navigate · Enter select · Esc cancel\n\n"
                            "     Alpha\n"
                            "  ❯ Beta\n"
                            "  Thinking  (←→ to switch)\n"
                            "     Low    [ High ]    Max\n")
        sent_keys = []

        after_screen = ("  Select a model  (type to search)\n"
                        "  ↑↓ navigate · Enter select · Esc cancel\n\n"
                        "     Alpha\n"
                        "     Beta\n"
                        "  Thinking  (←→ to switch)\n"
                        "     Low      High    [ Max ]\n")

        def recording(pane_id, key, pane=None):
            sent_keys.append(key)
            if key == "Right":
                self.tmux.output = after_screen

        self.tmux.send_key = recording
        result = self.client.post("/api/panes/%254/prompt/choose",
                                  json={"toggle": 0, "value": 2}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(sent_keys, ["Right"])
        after = self.client.get("/api/panes/%254/prompt").json()["prompt"]
        self.assertEqual(after["toggles"][0]["active"], 2)

    def test_mobile_gets_a_button_for_enter(self):
        """手机上没法敲回车：需要 Enter 确认的界面要有一颗明确的按钮。"""
        root = Path(__file__).resolve().parents[1]
        page = (root / "web" / "index.html").read_text()
        self.assertIn('id="prompt-card-enter"', page)
        self.assertIn(".prompt-btn-go", page)
        script = (root / "web" / "assets" / "prompt-card.js").read_text()
        self.assertIn("enterButton.onclick = () => chooseByKey('enter')", script)
        self.assertIn("data.kind === 'toggle'", script, "开关类界面总要确认按钮")

    def test_a_click_paints_before_the_round_trip(self):
        """点下去先在卡片上生效（乐观），再等 pane 的真实状态对齐。"""
        root = Path(__file__).resolve().parents[1]
        script = (root / "web" / "assets" / "prompt-card.js").read_text()
        self.assertIn("let optimistic = null", script)
        self.assertIn("optimistic = {toggle: toggleIndex, value, at: Date.now()}", script)
        self.assertIn("paintOptimistic()", script)
        self.assertIn("settleOptimistic(", script)
        # 过了窗口还没对上就退回真值，并且说清楚
        self.assertIn("屏幕没有变化，再点一次或切到终端", script)
        page = (root / "web" / "index.html").read_text()
        self.assertIn(".prompt-option.picked", page)

    def test_the_prompt_card_is_off_until_the_preference_says_on(self):
        """实验性功能：默认关闭时连 /prompt 都不该解析，choose 直接 409。"""
        self.login()
        # 这个用例断言的是"关掉之后"的行为，先把偏好摆到 off（别依赖库里现有的值）
        self.assertEqual(self.client.put("/api/prefs", json={"prompt_card": "off"}).status_code, 200)
        idle = self.client.get("/api/panes/%254/prompt").json()
        self.assertIsNone(idle["prompt"])
        self.assertTrue(idle.get("disabled"), "默认关闭要明确告诉前端")

        self.assertEqual(self.client.put("/api/prefs", json={"prompt_card": "on"}).status_code, 200)
        self.tmux.output = ("  Trust this folder?\n"
                            "  ↑↓ navigate · Enter select · Esc exit\n\n"
                            "  ❯ Trust and continue\n"
                            "     Exit\n")
        live = self.client.get("/api/panes/%254/prompt").json()
        self.assertIsNotNone(live["prompt"])
        self.assertFalse(live.get("disabled"))

        self.assertEqual(self.client.put("/api/prefs", json={"prompt_card": "off"}).status_code, 200)
        off = self.client.post("/api/panes/%254/prompt/choose", json={"index": 0})
        self.assertEqual(off.status_code, 409)
        self.assertIn("设置", off.json()["detail"])

    def test_the_preference_rejects_unknown_values(self):
        self.login()
        bad = self.client.put("/api/prefs", json={"prompt_card": "maybe"})
        self.assertEqual(bad.status_code, 400)

    def test_settings_exposes_the_experimental_switch(self):
        self.login()
        panel = self.client.get("/assets/settings.js").text   # 设置面板的标记在脚本里
        self.assertIn('data-ts-prompt="off"', panel)
        self.assertIn('data-ts-prompt="on"', panel)
        self.assertIn("交互菜单卡片（实验性）", panel)
        page = self.client.get("/").text
        # 控制台据此决定要不要轮询
        self.assertIn("tmuxweb:prompt-card", page)
        self.assertIn("TmuxPrompt.stop()", page)

    def test_the_card_answers_the_arrow_keys_the_switch_advertises(self):
        """分段开关写着 left/right to switch：卡片必须真的处理 ←→，否则键盘按下去没反应。"""
        script = (Path(__file__).resolve().parents[1] / "web" / "assets" / "prompt-card.js").read_text()
        self.assertIn("event.key === 'ArrowLeft'", script)
        self.assertIn("event.key === 'ArrowRight'", script)
        self.assertIn("chooseToggle(toggle.index, next)", script)
        # ↑↓ 只在有列表项时才动光标：只有一个开关时 options 是空的，取模会得到 NaN
        self.assertIn("(event.key === 'ArrowDown' || event.key === 'ArrowUp') && hasOptions", script)
        self.assertIn("if (target) target.focus()", script)

    def test_the_card_repaints_when_the_switch_value_changes(self):
        """只比标题/选项数/光标的话，←→ 切完高亮停在旧值，看起来像没生效。"""
        script = (Path(__file__).resolve().parents[1] / "web" / "assets" / "prompt-card.js").read_text()
        self.assertIn("const toggleKey =", script)
        self.assertIn("toggleKey(prompt) === toggleKey(data.prompt)", script)

    def test_sending_hides_the_attachment_strip_right_away(self):
        """按 Enter 那一刻条带就该收起来，而不是等 POST 回来（那一秒图片还杵着）。"""
        self.login()
        page = self.client.get("/").text
        send = page[page.index("async function sendInput("):]
        send = send[:send.index("async function createShare(")]
        self.assertIn("prompt?takeAttachments():[]", send, "先收条带")
        self.assertLess(send.index("prompt?takeAttachments():[]"),
                        send.index("/input"), "要在发请求之前收")
        self.assertNotIn("clearAttachments()", send, "不能再等成功后清")
        self.assertIn("restoreAttachments(attachments)", send, "失败要放回去")
        self.assertIn("input.value=typed", send, "回填不含附件前缀，否则会拼两遍")

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

    def test_a_switch_only_screen_answers_422_not_a_server_error(self):
        """只有开关、没有选项时点「选第 0 项」不该冒成 503 的 'no options'。"""
        self.enable_prompt_card()
        self.tmux.output = ("  形态\n"
                            "  ←→ to switch · Enter confirm · Esc cancel\n"
                            "   Thinking\n"
                            "      Low    [ High ]    Max\n")
        sent_keys = []
        self.tmux.send_key = lambda pane_id, key, pane=None: sent_keys.append(key)
        response = self.client.post("/api/panes/%254/prompt/choose", json={"index": 0})
        self.assertEqual(response.status_code, 422)
        self.assertIn("开关", response.json()["detail"])
        self.assertEqual(sent_keys, [], "出错时一个键都不该发")

    def test_enter_on_a_switch_only_screen_confirms_it(self):
        self.enable_prompt_card()
        self.tmux.output = ("  ←→ to switch · Enter confirm · Esc cancel\n"
                            "   Thinking\n"
                            "      Low    [ High ]    Max\n")
        sent_keys = []
        original = self.tmux.output

        def recording(pane_id, key, pane=None):
            sent_keys.append(key)
            if key == "Enter":
                self.tmux.output = "  完成\n"

        self.tmux.send_key = recording
        result = self.client.post("/api/panes/%254/prompt/choose", json={"key": "enter"}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(sent_keys, ["Enter"])
        self.assertTrue(original)
        self.assertIsNone(self.client.get("/api/panes/%254/prompt").json()["prompt"])

    def test_clicking_the_value_it_already_has_sends_nothing(self):
        self.enable_prompt_card()
        self.tmux.output = ("  Select a model  (type to search)\n"
                            "  ↑↓ navigate · Enter select · Esc cancel\n\n"
                            "     Alpha\n"
                            "  ❯ Beta\n"
                            "  Thinking  (←→ to switch)\n"
                            "     Low    [ High ]    Max\n")
        sent_keys = []
        self.tmux.send_key = lambda pane_id, key, pane=None: sent_keys.append(key)
        result = self.client.post("/api/panes/%254/prompt/choose",
                                  json={"toggle": 0, "value": 1}).json()
        self.assertTrue(result["ok"])
        self.assertEqual(sent_keys, [])

    def test_choosing_without_a_menu_is_a_conflict(self):
        self.enable_prompt_card()
        self.tmux.output = "idle\n"
        response = self.client.post("/api/panes/%254/prompt/choose", json={"index": 0})
        self.assertEqual(response.status_code, 409)

    def test_escape_is_always_available(self):
        self.enable_prompt_card()
        self.tmux.output = ("  Select a model  (type to search)\n"
                            "  ↑↓ navigate · Enter select · Esc cancel\n\n"
                            "  ❯ Alpha\n"
                            "    Beta\n")
        sent_keys = []

        def recording(pane_id, key, pane=None):
            sent_keys.append(key)
            if key == "Escape":
                self.tmux.output = "idle\n"

        self.tmux.send_key = recording
        result = self.client.post("/api/panes/%254/prompt/choose", json={"key": "esc"}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(sent_keys, ["Escape"])
        self.assertIsNone(self.client.get("/api/panes/%254/prompt").json()["prompt"])

    def test_pasted_images_go_through_the_attachment_pipeline(self):
        """Ctrl/Cmd+V 的图片要走和点「附件」同一条路：上传 + 缩略图。"""
        script = self.client.get("/assets/composer-files.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("'paste'", script.text)
        self.assertIn("clipboardData", script.text)
        self.assertIn("pastedNameFor", script.text)
        self.assertIn("pasted-", script.text)

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
        for name in ("index.html", "home.html"):
            html = (web / name).read_text()
            self.assertIn("TmuxMarkdownCjk.fix(", html, name)
            self.assertIn("TmuxMarkdownCjk.strip(", html, name)
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
        for name in ("index.html", "home.html"):
            html = (web / name).read_text()
            self.assertIn("/assets/video-cards.css", html, name)
        self.assertIn("/assets/video-cards.js", (web / "home.html").read_text())
        self.assertIn("/assets/console.bundle.js?v=", (web / "index.html").read_text())
        self.assertIn("TmuxVideo", self.client.get("/assets/console.bundle.js").text)
        self.assertIn("enhanceVideos(root)", (web / "index.html").read_text())
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
        """index.html 的内联 <script type=module> 抽出来必须过 node --check。"""
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        import re
        page = Path(__file__).resolve().parent.parent.joinpath("web", "index.html").read_text()
        match = re.search(r'<script type="module">(.*?)</script>', page, re.S)
        self.assertIsNotNone(match, "找不到内联 module 脚本")
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as handle:
            handle.write(match.group(1))
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
        page = self.client.get("/").text
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
        page = self.client.get("/").text
        for marker in ('id="agent-progress"', "function syncProgress(", "function sinceSeconds(",
                       "function humanElapsed(", "'thinking'", "'idle'", "function agentWorking(",
                       "agent-progress.working"):
            self.assertIn(marker, page)

    def test_share_and_fullscreen_entries_are_icons_on_phones(self):
        self.login()
        page = self.client.get("/").text
        # the share management entry reuses the arrow icon, both actions are icon-only
        self.assertIn('<a class="icon-btn" href="/shares" title="管理分享"', page)
        self.assertIn('href="/shares" title="管理分享" aria-label="管理分享" style="text-decoration:none"><svg class="ico"><use href="#i-external"/></svg>', page)
        # the fullscreen label lives in a span so the phone layout can hide it
        self.assertIn('<svg class="ico"><use href="#i-fullscreen"/></svg><span>对话全屏</span>', page)
        self.assertIn(".fullscreen-btn span{display:none}", page)
        # the top bar shrinks instead of pushing its buttons off screen
        self.assertIn(".session{flex:1 1 auto;min-width:0}", page)
        self.assertIn(".top-actions{flex:none}", page)

    def test_share_dialog_is_a_fixed_size_sheet_on_phones(self):
        self.login()
        page = self.client.get("/").text
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
        response = self.client.get("/")
        page = response.text
        for marker in ('id="load-earlier"', 'function loadEarlier(', 'TRANSCRIPT_PAGE_MAX',
                       'id="earlier-bar"', 'state.transcriptLimit'):
            self.assertIn(marker, page)
        # the share action is an icon only (arrow to the upper right)
        self.assertIn('<button class="share-btn icon-only" id="share-button"', page)
        self.assertIn('href="#i-external"', page)
        self.assertNotIn("</svg>分享", page)

    def test_markdown_links_open_an_in_console_preview(self):
        self.login()
        response = self.client.get("/")
        page = response.text
        for marker in ('id="md-viewer"', 'id="md-toggle"', 'function openMarkdownViewer(',
                       'function looksLikeMarkdownLink(', 'MD_VIEWER_EXTS'):
            self.assertIn(marker, page)

    def test_rendered_transcript_links_are_rewritten_to_the_raw_endpoint(self):
        self.login()
        response = self.client.get("/")
        self.assertIn("/files/raw?path=", response.text)
        self.assertIn("function rewriteLocalLinks(", response.text)
        self.assertIn("rewriteLocalLinks(host)", response.text)

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
        page = self.client.get("/").text
        self.assertIn('/assets/composer-files.css', page)
        self.assertIn('/assets/composer-files.js', page)
        self.assertIn('<div class="composer-card"><div class="cf-bar">', page)
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
        page = self.client.get("/").text
        self.assertIn('<link rel="stylesheet" href="/assets/pane-preview.css?v=1">', page)
        self.assertIn("/assets/console.bundle.js?v=", page)
        self.assertIn("TmuxPanePreview", self.client.get("/assets/console.bundle.js").text)
        for marker in ("function setupPanePreview(){", "setupRecentPreview();",
                       "localStorage.getItem('tmux-preview-delay')",
                       "seconds*1000", "TmuxPanePreview.bind($('pane-list')",
                       "selector:'.recent-row',paneOf:element=>element.dataset.row",
                       "new URLSearchParams(location.search).has('preview')",
                       "previewFromTranscript(data)"):
            self.assertIn(marker, page)
        # 不在 /api/home 的 pane 悬停时才取一次，60 秒内复用
        module = self.client.get("/assets/pane-preview.js").text
        for marker in ("const canHover = !!(window.matchMedia", "(hover: hover) and (pointer: fine)",
                       "function previewFor(paneId)", "function bind(container, config)",
                       "if (!canHover && !options.force) return;", "FETCH_TTL = 60_000",
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
                                       share_db=":memory:", home_db=":memory:",
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
        page = self.client.get("/").text
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
        page = self.client.get("/").text
        self.assertIn('/assets/downloads.css', page)
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

    def test_chat_messages_turn_paths_into_links(self):
        """独立的路径后处理：/assets/path-links.js，控制台和 /preview 都用它。"""
        self.login()
        page = self.client.get("/").text
        self.assertIn('/assets/path-links.css', page)
        self.assertIn('/assets/path-links.js', page)
        self.assertIn("function enhancePaths(root){", page)
        # 懒处理：渲染时不扫，碰到哪条消息才处理哪条（长会话别每次全量扫文本）
        self.assertIn("function enhancePathsLazy(container){", page)
        self.assertIn("container.dataset.pathsBound='1';", page)
        self.assertIn("'pointerover',touch,true", page)
        self.assertIn("enhancePathsLazy(host);", page)
        self.assertNotIn("rewriteLocalLinks(host);enhancePaths(host);", page)
        self.assertIn("onDir:path=>{openFiles(path);}", page)
        self.assertIn("onMd:path=>openMarkdownViewer(", page)
        self.assertIn('id="md-download"', page)

        preview = self.client.get("/preview?pane=%251").text
        self.assertIn('/assets/path-links.js', preview)
        self.assertIn("function enhancePaths(root){", preview)

        module = self.client.get("/assets/path-links.js").text
        self.assertIn("function activateAt(x, y, scope)", module)
        self.assertIn("__plinkOpen", module)
        # 悬停气泡：文件名 + 大小 + 预览/下载；log/txt 这类点开是预览而不是直接下载
        self.assertIn("TEXT_EXT", module)
        self.assertIn("plink-card-actions", module)
        self.assertIn('data-plink-act="download"', module)
        self.assertIn("downloadHref", module)
        self.assertIn("onPreview:path=>openMarkdownViewer(", page)
        self.assertIn(".plink-card{position:fixed", self.client.get("/assets/path-links.css").text)
        for marker in ("window.TmuxPaths = {enhance, rawHref, lookup, activateAt, downloadHref, hideCard};",
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
                                           share_db=":memory:", remote_store=FakeRemoteStore(),
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
    """POST /input (and the relay) must report pasted_not_submitted instead of a silent sent."""

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
            share_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))

    def login(self, client):
        response = client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_input_reports_pasted_not_submitted_with_200(self):
        client = self.client_for(self.NeverSubmits())
        self.login(client)
        response = client.post("/api/panes/%254/input", json={"text": "hello", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pasted_not_submitted")
        self.assertFalse(response.json()["submitted"])

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

    def test_relay_message_reports_pasted_not_submitted(self):
        client = self.client_for(self.NeverSubmits())
        self.login(client)
        response = client.post("/api/relay/message", json={"target": "%4", "text": "请先跑测试"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pasted_not_submitted")
        self.assertFalse(response.json()["submitted"])

    def guest_for_share(self, tmux, slug="share001"):
        tmux.panes[0]["command"] = "claude"
        client = self.client_for(tmux)
        self.login(client)
        created = client.post("/api/shares", json={
            "pane_id": "%4", "slug": slug, "password": "", "expires_in": 3600,
        })
        self.assertEqual(created.status_code, 201)
        return TestClient(client.app), slug

    def test_share_input_reports_pasted_not_submitted_with_200(self):
        guest, slug = self.guest_for_share(self.NeverSubmits())
        response = guest.post(f"/share/{slug}/api/input", json={"text": "继续分析", "submit": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pasted_not_submitted")
        self.assertFalse(response.json()["submitted"])

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


if __name__ == "__main__":
    unittest.main()
