import hashlib
import io
import os
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
        self.created = []
        self.auto_reply = None

    def create_session(self, name, path=""):
        try:
            name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        if any(pane["session"] == name for pane in self.panes):
            raise TmuxError("session already exists: %s" % name)
        if path and not Path(path).is_dir():
            raise TmuxError("no such directory: %s" % path)
        self.created.append((name, path))
        pane = {"id": "%12", "pane_id": "%12", "target": "%s:0.0" % name, "session": name,
                "window": "0", "window_name": "shell", "index": "0", "command": "zsh",
                "title": "new session", "path": path or "/work/repo", "active": True}
        self.panes.append(pane)
        return {"session": name, "path": path}

    def list_panes(self):
        return self.panes

    def capture(self, pane_id, history=500):
        self.validate_pane(pane_id)
        return self.output

    def capture_ansi(self, pane_id, history=500):
        self.validate_pane(pane_id)
        return "\x1b[32mhello\x1b[0m\n› Ask Codex to do anything\n"

    def send(self, pane_id, text, submit=True):
        self.validate_pane(pane_id)
        self.sent.append((pane_id, text, submit))
        if self.auto_reply:
            match = re.search(r"TMUXWEB_REPLY_([0-9a-f]{24}):", text)
            if match:
                self.output = "hello\n" + match.group(0) + ' "' + self.auto_reply + '"\n'

    def transcript(self, pane_id, limit=200):
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
    def check(self): return "TMUX_REMOTE_OK"
    def create_session(self, name, path=""):
        name = normalize_session_name(name)
        self.created.append((name, path))
        FakeSSHRunner.created = (name, path)
        return {"session": name, "path": path}
    def list_panes(self, timeout=15):
        extra = [{"id": pane_ref(self.host["id"], "%11"), "pane_id": "%11", "host_id": self.host["id"],
                  "target": "%s:0.0" % name, "session": name, "window": "0", "window_name": "shell",
                  "index": "0", "command": "zsh", "title": "new", "path": path or "/srv/work",
                  "active": True, "width": 100, "height": 30}
                 for name, path in getattr(self, "created", [])]
        return extra + [{"id": pane_ref(self.host["id"], "%9"), "pane_id": "%9", "host_id": self.host["id"],
                 "target": "remote:0.0", "session": "remote", "window": "0", "window_name": "agent",
                 "index": "0", "command": "claude", "title": "Remote Claude", "path": "/srv/work",
                 "active": True, "width": 100, "height": 30}]
    def capture_ansi(self, pane_id, history=5000): return FakeSSHRunner.screen
    def capture(self, pane_id, history=5000):
        return ANSI_RE.sub("", FakeSSHRunner.screen)
    def snapshot_ansi(self, pane_id, history=5000, known_digest=None):
        content = self.capture_ansi(pane_id, history)
        digest = hashlib.sha1(content.encode()).hexdigest()
        return digest, (None if digest == known_digest else content)
    def transcript(self, pane_id, limit=200):
        return {"pane": pane_id, "provider": "claude", "session_id": "remote-session-1", "mtime": 7,
                "messages": [{"id": "r1", "role": "user", "text": "远端问题", "timestamp": None}]}
    def zip_files(self, paths, base, max_bytes, max_files):
        FakeSSHRunner.zipped = (tuple(paths), base, max_bytes, max_files)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("remote.txt", b"remote bytes")
        return buffer.getvalue()

    def send(self, pane_id, text, submit=True):
        self.sent.append((self.host["id"], pane_id, text, submit))
        if FakeSSHRunner.auto_reply:
            match = re.search(r"TMUXWEB_REPLY_([0-9a-f]{24}):", text)
            if match:
                FakeSSHRunner.screen = "❯ " + match.group(0) + ' "' + FakeSSHRunner.auto_reply + '"\n'


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
        # iOS zooms the page for a field under 16px and never zooms back out
        self.assertIn(".terminal-input-row input{font-size:16px", page)
        # the terminal view keeps its header row while composing, so the terminal shrinks
        # with the keyboard instead of growing by the 58px the header would give back
        self.assertIn(".app.terminal-view.mobile-composing .topbar{visibility:visible", page)
        self.assertIn(".app.terminal-view.mobile-composing .main{grid-template-rows:58px", page)
        # dismissing the keyboard must refit even when visualViewport stays quiet
        self.assertIn("scheduleTerminalFit();fitCapture();", page)

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
        self.tmux.send_key = lambda pane_id, key: sent.append((pane_id, key))
        response = self.client.post("/api/panes/%254/interrupt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["key"], "C-q")
        self.assertEqual(sent, [("%4", "C-q")])

    def test_escape_can_be_sent_to_interrupt_a_turn(self):
        self.login()
        sent = []
        self.tmux.send_key = lambda pane_id, key: sent.append((pane_id, key))
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
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.12])
        self.assertEqual(calls[3][0], ("send-keys", "-t", "%4", "C-m"))

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

    def test_terminal_websocket_is_bidirectional_and_resizable(self):
        self.login()
        with self.client.websocket_connect("/ws/terminal/%254?cols=100&rows=30") as websocket:
            self.assertEqual(websocket.receive_bytes(), b"\x1b[32mTERM_READY\x1b[0m")
            websocket.send_text(json.dumps({"type": "input", "data": "hello\r"}))
            websocket.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        session = self.terminals.sessions[0]
        self.assertEqual(session.pane_id, "%4")
        self.assertEqual(session.inputs, [b"hello\r"])
        self.assertEqual(session.resizes, [(120, 40)])
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
        self.assertIn("state.mode==='chat'&&!isMobile()", response.text)
        self.assertIn("mode==='chat'&&state.selected&&!isMobile()", response.text)
        self.assertIn("viewport-fit=cover", response.text)
        self.assertIn('rel="manifest"', response.text)
        self.assertIn("apple-mobile-web-app-capable", response.text)
        self.assertIn("serviceWorker.register('/sw.js')", response.text)
        self.assertIn("-webkit-text-size-adjust:100%", response.text)
        self.assertIn(".chat-message.assistant{justify-content:flex-start}", response.text)
        self.assertIn("background:#fff;color:var(--ink);font-size:15px", response.text)
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
        self.assertIn("pane!==state.selected)return", response.text)
        # the path bar names the machine the listing came from
        self.assertIn('id="file-host"', response.text)

        # creating a tmux session on a host that has none
        self.assertIn('id="session-dialog"', response.text)
        self.assertIn('data-new-session="', response.text)
        self.assertIn('<symbol id="i-plus"', response.text)
        self.assertIn("async function submitSessionForm(", response.text)
        self.assertIn("api('/api/sessions'", response.text)
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
        # definition + footer button + toolbar button (which has two call sites)
        self.assertEqual(response.text.count("downloadArchive("), 4)
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
        self.tmux.panes[0]["command"] = "zsh"
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
                         {"list_agents", "ask_agent", "send_agent", "read_agent",
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
        self.assertEqual(len(open_mcp.json()["result"]["tools"]), 10)
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

    def test_session_creation_validates_name_path_and_duplicates(self):
        self.login()
        self.assertEqual(self.client.post("/api/sessions", json={"name": "bad:name"}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions", json={"name": "x y"}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions",
                                          json={"name": "ok", "path": "/nope/missing"}).status_code, 422)
        self.assertEqual(self.client.post("/api/sessions", json={"name": "codex"}).status_code, 409)
        self.assertEqual(self.client.post("/api/sessions", json={"host": "deadbeefdeadbeef",
                                                                 "name": "ok"}).status_code, 404)

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
        self.tmux.transcript = lambda pane_id, limit=200, *args: dict(answered)
        idle = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertFalse(idle["waiting"])
        waiting = {"provider": "claude", "mtime": 1, "messages": [
            {"id": "u2", "role": "user", "text": "再来一次", "timestamp": "2026-10-27T22:10:00Z"},
        ]}
        self.tmux.transcript = lambda pane_id, limit=200, *args: dict(waiting)
        busy = self.client.get("/api/panes/%254/transcript?limit=20").json()
        self.assertTrue(busy["waiting"])
        self.assertEqual(busy["waiting_since"], "2026-10-27T22:10:00Z")

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


if __name__ == "__main__":
    unittest.main()
