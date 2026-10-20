import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from tmux_web import ANSI_RE, TmuxBackend, create_app
from tests.fake_terminal import FakeTerminalFactory
from remote_hosts import pane_ref, host_key_fingerprint


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
    def __init__(self): self.hosts = {}
    def add_host(self, **data):
        host = {"id": "0123456789abcdef", "enabled": True, **data}
        credential = host.pop("credential")
        self.hosts[host["id"]] = (host, credential)
        return host
    def list_hosts(self): return [item[0] for item in self.hosts.values()]
    def get_host(self, host_id, include_credential=False):
        host, credential = self.hosts[host_id]
        return {**host, **({"credential": credential} if include_credential else {})}
    def delete_host(self, host_id): return self.hosts.pop(host_id, None) is not None


class FakeSSHRunner:
    sent = []
    def __init__(self, host): self.host = host
    def check(self): return "TMUX_REMOTE_OK"
    def list_panes(self):
        return [{"id": pane_ref(self.host["id"], "%9"), "pane_id": "%9", "host_id": self.host["id"],
                 "target": "remote:0.0", "session": "remote", "window": "0", "window_name": "agent",
                 "index": "0", "command": "claude", "title": "Remote Claude", "path": "/srv/work",
                 "active": True, "width": 100, "height": 30}]
    def capture(self, pane_id, history=5000): return "\x1b[32mREMOTE_OK\x1b[0m"
    def send(self, pane_id, text, submit=True): self.sent.append((self.host["id"], pane_id, text, submit))


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
        self.assertEqual(self.client.get(f"/api/panes/{pane_id}/transcript").status_code, 409)

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
        self.assertIn("if(state.selected!==id||state.mode!=='chat')return;showError('')", response.text)
        self.assertIn("api('/api/hosts')", response.text)
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
        self.assertIn("!pending.baselineIds.has(String(message.id))", response.text)
        self.assertIn("messageTextKey(message.text).includes(messageTextKey(pending.text))", response.text)
        self.assertIn("发送中", response.text)
        self.assertIn("已发送 ✓", response.text)
        self.assertIn("发送失败", response.text)
        self.assertIn("pending-wave", response.text)

        hosts_page = self.client.get("/hosts")
        self.assertIn("function esc(value)", hosts_page.text)
        self.assertIn("${esc(h.name)}", hosts_page.text)

        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["display"], "standalone")
        worker = self.client.get("/sw.js")
        self.assertEqual(worker.status_code, 200)
        self.assertIn("no-store", worker.text)

    def test_ansi_sequences_can_be_removed_without_changing_columns(self):
        raw = "| A   | B |\n\x1b[38;5;231m| 123 | x |\x1b[0m"
        self.assertEqual(ANSI_RE.sub("", raw), "| A   | B |\n| 123 | x |")

if __name__ == "__main__":
    unittest.main()
