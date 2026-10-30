"""A favourite whose pane dropped keeps showing the last thing it said."""
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

from test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux
from tmux_web import create_app

PASSWORD = "unit-test-admin-password"


class HomeOfflineCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {
            "TMUX_WEB_HOME_ENTRY_SECONDS": "0",          # never serve a fresh hit: always re-read
            "TMUX_WEB_PASSWORD_DB": str(Path(self.folder.name) / "passwords.json"),
        })
        self.env.start()
        self.tmux = FakeTmux()
        self.tmux.panes[0]["command"] = "claude"   # the fake only serves a transcript for claude
        self.client = TestClient(create_app(
            self.tmux, password=PASSWORD, session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner))
        self.client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def favourite(self, pane):
        response = self.client.post("/api/home/favorites", json={"pane": pane})
        self.assertEqual(response.status_code, 201, response.text)

    def test_a_dropped_pane_still_shows_its_cached_preview(self):
        self.favourite("%4")
        online = self.client.get("/api/home").json()["favorites"][0]
        self.assertIn(online["status"], {"idle", "busy", "unknown"})
        self.assertIsNotNone(online["preview"], online)

        # the pane is gone: the card must read offline, and keep the text it had
        self.tmux.panes = []
        offline = self.client.get("/api/home").json()["favorites"][0]
        self.assertEqual(offline["status"], "offline")
        self.assertTrue(offline.get("cached"), offline)
        self.assertGreater(offline.get("cached_at") or 0, 0)
        self.assertEqual(offline["preview"]["text"], online["preview"]["text"])

    def test_no_cache_means_no_preview(self):
        self.favourite("%4")
        self.tmux.panes = []
        entry = self.client.get("/api/home").json()["favorites"][0]
        self.assertEqual(entry["status"], "offline")
        self.assertFalse(entry.get("cached"))
        self.assertIsNone(entry["preview"])

    def test_the_board_page_marks_cached_previews(self):
        page = self.client.get("/home").text
        self.assertIn("function cardPreview(", page)
        self.assertIn("离线 · 缓存", page)
        self.assertIn("favorite.cached", page)
        self.assertIn("#hover-preview.offline #hp-role", page)


if __name__ == "__main__":
    unittest.main()
