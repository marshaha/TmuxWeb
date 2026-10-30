"""Settings: rotating the console password and exporting configuration."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fastapi.testclient import TestClient

from auth_store import PasswordStore, hash_password, verify_password
from test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux
from tmux_web import create_app


class PasswordStoreTests(unittest.TestCase):
    def test_a_missing_file_falls_back_to_the_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PasswordStore(Path(folder) / "passwords.json")
            self.assertEqual(store.encoded(), "")
            self.assertEqual(store.material(), "")
            self.assertFalse(store.verify("anything"))

    def test_a_stored_password_is_hashed_and_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "passwords.json"
            store = PasswordStore(path)
            store.set("second-password")
            self.assertNotIn("second-password", path.read_text())
            self.assertTrue(PasswordStore(path).verify("second-password"))
            self.assertFalse(PasswordStore(path).verify("first-password"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_verify_rejects_junk(self):
        self.assertFalse(verify_password("x", "not-a-hash"))
        self.assertTrue(verify_password("x", hash_password("x")))


class SettingsApiTests(unittest.TestCase):
    PASSWORD = "unit-test-admin-password"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.password_db = str(Path(self.folder.name) / "passwords.json")
        self.env = patch.dict("os.environ", {"TMUX_WEB_PASSWORD_DB": self.password_db})
        self.env.start()
        self.tmux = FakeTmux()
        self.client = self.make_app()
        self.login(self.client, self.PASSWORD)

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def make_app(self):
        return TestClient(create_app(
            self.tmux,
            password=self.PASSWORD,
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))

    def login(self, client, password):
        return client.post("/login", data={"password": password}, follow_redirects=False)

    def test_settings_report_where_the_password_lives(self):
        info = self.client.get("/api/settings").json()
        self.assertEqual(info["password_source"], "env")
        self.assertIsInstance(info["hosts"], int)
        self.assertNotIn(self.PASSWORD, str(info))

    def test_the_settings_page_needs_a_login(self):
        anon = self.make_app()
        self.assertEqual(anon.get("/api/settings").status_code, 401)
        self.assertEqual(anon.post("/api/settings/password",
                                   json={"current": self.PASSWORD, "next": "new-password"}).status_code, 401)

    def test_a_wrong_or_too_short_password_is_refused(self):
        wrong = self.client.post("/api/settings/password",
                                 json={"current": "nope", "next": "new-password"})
        self.assertEqual(wrong.status_code, 403)
        short = self.client.post("/api/settings/password",
                                 json={"current": self.PASSWORD, "next": "123"})
        self.assertEqual(short.status_code, 400)
        same = self.client.post("/api/settings/password",
                                json={"current": self.PASSWORD, "next": self.PASSWORD})
        self.assertEqual(same.status_code, 400)

    def test_changing_the_password_rotates_the_login(self):
        response = self.client.post("/api/settings/password",
                                    json={"current": self.PASSWORD, "next": "second-password"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["password_source"], "file")
        # the client that asked keeps working (the response re-issues its cookie)
        self.assertEqual(self.client.get("/api/panes").status_code, 200)
        # a fresh browser needs the new password; the old one stops working
        other = self.make_app()
        self.assertEqual(self.login(other, self.PASSWORD).status_code, 401)
        self.assertEqual(self.login(other, "second-password").status_code, 303)
        self.assertEqual(other.get("/api/panes").status_code, 200)
        self.assertEqual(other.get("/api/settings").json()["password_source"], "file")

    def test_a_failed_edit_restores_the_jump_hop_too(self):
        """A broken credential must not leave a host half-edited (the hop used to survive the rollback)."""
        first = self.client.post("/api/remote-hosts", json={
            "name": "hop", "hostname": "10.7.7.1", "username": "root", "auth_type": "agent",
            "verify": False}).json()
        second = self.client.post("/api/remote-hosts", json={
            "name": "target", "hostname": "10.7.7.2", "username": "root", "auth_type": "agent",
            "verify": False}).json()
        with patch.object(FakeSSHRunner, "check", side_effect=RuntimeError("unreachable")):
            failed = self.client.put(f"/api/remote-hosts/{second['id']}",
                                     json={"jump_host_id": first["id"]})
        self.assertEqual(failed.status_code, 424)
        stored = [host for host in self.client.get("/api/remote-hosts").json()["hosts"]
                  if host["id"] == second["id"]][0]
        self.assertEqual(stored.get("jump_host_id", ""), "")
        self.assertEqual(stored.get("jump_path", ""), "")

    def test_the_encrypted_export_carries_the_credentials(self):
        created = self.client.post("/api/remote-hosts", json={
            "name": "export-target", "hostname": "10.9.9.9", "username": "root",
            "port": 22, "auth_type": "password", "credential": "host-secret-value",
            "verify": False,
        })
        self.assertEqual(created.status_code, 201, created.text)
        response = self.client.get("/api/settings/export")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        hosts = response.json()["hosts"]
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["credential"], "host-secret-value")
        self.assertIn("jump_name", hosts[0])
        anon = self.make_app()
        self.assertEqual(anon.get("/api/settings/export").status_code, 401)

    def test_the_export_material_carries_no_credentials(self):
        created = self.client.post("/api/remote-hosts", json={
            "name": "export-target", "hostname": "10.9.9.9", "username": "root",
            "port": 22, "auth_type": "password", "credential": "host-secret-value",
            "verify": False,
        })
        self.assertEqual(created.status_code, 201, created.text)
        payload = self.client.get("/api/remote-hosts").json()
        text = str(payload).lower()
        for forbidden in ("host-secret-value", "private_key", "passphrase"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(payload["hosts"])



class SettingsPageWiringTests(unittest.TestCase):
    """The console page must carry the three things the settings work added."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {
            "TMUX_WEB_PASSWORD_DB": str(Path(self.folder.name) / "passwords.json")})
        self.env.start()
        self.client = TestClient(create_app(
            FakeTmux(), password="unit-test-admin-password", session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner))
        self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.page = self.client.get("/?pane=%2561").text

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_the_add_to_grid_icon_lives_in_the_chat_header(self):
        header = self.page[self.page.index('<div class="top-actions">'):]
        header = header[:header.index("</header>")]
        self.assertIn('id="grid-add"', header)
        self.assertIn('href="#i-grid"', header)
        # it sits with the other chat actions, not in the sidebar footer
        footer = self.page[self.page.index('<footer class="side-foot">'):]
        footer = footer[:footer.index("</footer>")]
        self.assertNotIn("grid-add", footer)

    def test_a_failed_message_offers_to_resend(self):
        self.assertIn('data-resend=', self.page)
        self.assertIn("重发", self.page)
        self.assertIn("resendPending", self.page)
        self.assertIn(".retry-send{", self.page)

    def test_the_panel_script_is_shared_and_carries_the_markup(self):
        """The panel lives in /assets/settings.js so the console and /grid share one copy."""
        script = self.client.get("/assets/settings.js").text
        for marker in ("修改密码", "导入 / 导出", "退出登录", "/api/settings/password",
                       "/api/settings/export", "encryptBundle", "decryptBundle", "PBKDF2", "AES-GCM",
                       "导出密码至少 6 位", "密码不对，或者文件被改过"):
            self.assertIn(marker, script)
        # one section with two tabs, not two separate rows
        self.assertEqual(script.count('class="ts-row"'), 2)
        self.assertIn("data-ts-tab=\"export\"", script)
        self.assertIn("data-ts-tab=\"import\"", script)
        self.assertIn('id="ts-import-file" type="file"', script)
        self.assertIn("未选择文件", script)

    def test_every_page_with_a_settings_button_loads_it(self):
        for path in ("/", "/grid"):
            page = self.client.get(path).text
            self.assertIn('/assets/settings.js', page, path)
            self.assertIn('id="settings-open"', page, path)
            self.assertNotIn('action="/logout"', page, path)   # logging out moved into the panel

    def test_the_settings_panel_can_change_the_password_and_log_out(self):
        script = self.client.get("/assets/settings.js").text
        for marker in ("'ts-dialog'", 'id="ts-pw-current"', 'id="ts-pw-new"',
                       'id="ts-pw-confirm"', 'data-ts-export', 'id="ts-logout"',
                       "fetch('/logout'", "location.href = '/login'"):
            self.assertIn(marker, script)
        page = self.client.get("/").text
        footer = page[page.index('<footer class="side-foot">'):]
        self.assertIn("settings-open", footer[:footer.index("</footer>")])

if __name__ == "__main__":
    unittest.main()
