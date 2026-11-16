"""Needs-you inbox API: list / count / open-marks-read / mark-one-read.

The app and the test share one sqlite file (the app's store holds its own
connection; the test seeds through a second one) so the endpoints are
exercised against real rows without driving a sweep.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from needs_you_inbox import NeedsYouInbox
from tmux_web import create_app

_PASSWORD = "unit-test-" + "admin-password"
_SECRET = "inbox-test-" + "secret"


class _EmptyBackend:
    """No panes anywhere: the sweep behind pane_alive answers at once."""

    def list_panes(self):
        return []


class _NoRemoteHosts:
    def list_hosts(self):
        return []

    def get_host(self, host_id, include_credential=False):
        raise KeyError(host_id)


class InboxApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.directory.name) / "inbox.db")
        self.client = TestClient(create_app(
            _EmptyBackend(),
            password=_PASSWORD,
            session_secret=_SECRET,
            share_db=":memory:",
            home_db=":memory:",
            needs_you_inbox_db=self.db_path,
            remote_store=_NoRemoteHosts(),
        ))
        self.store = NeedsYouInbox(self.db_path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def login(self):
        response = self.client.post(
            "/login", data={"password": _PASSWORD}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def seed(self, count=3):
        return [self.store.record(pane_id=f"localhost~%{index}", host_id="localhost",
                                  title="claude", kind="menu",
                                  summary=f"Do you want to proceed? — {index}",
                                  options=[{"label": "Yes", "meta": "", "number": "1"}])
                for index in range(count)]

    def test_endpoints_require_login(self):
        for path in ("/api/needs-you/inbox", "/api/needs-you/inbox/count"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(
            self.client.post("/api/needs-you/inbox/open").status_code, 401)

    def test_list_count_and_open(self):
        self.login()
        self.assertEqual(self.client.get("/api/needs-you/inbox/count").json(), {"count": 0})
        ids = self.seed()
        self.assertEqual(self.client.get("/api/needs-you/inbox/count").json(), {"count": 3})
        page = self.client.get("/api/needs-you/inbox").json()
        self.assertEqual([item["id"] for item in page["items"]], ids[::-1])
        self.assertIsNone(page["next_cursor"])
        # no live panes behind the stub backend: every row reports closed,
        # in place — the console renders "该 pane 已关闭", never a 404 page.
        self.assertTrue(all(item["pane_alive"] is False for item in page["items"]))
        self.assertEqual(page["items"][0]["summary"], "Do you want to proceed? — 2")
        opened = self.client.post("/api/needs-you/inbox/open").json()
        self.assertEqual(opened, {"acknowledged_count": 3, "count": 0})
        self.assertEqual(self.client.get("/api/needs-you/inbox/count").json(), {"count": 0})

    def test_list_pages_by_id_cursor(self):
        self.login()
        ids = self.seed(5)
        first = self.client.get("/api/needs-you/inbox?limit=2").json()
        self.assertEqual([item["id"] for item in first["items"]], ids[::-1][:2])
        second = self.client.get(
            f"/api/needs-you/inbox?limit=2&before={first['next_cursor']}").json()
        self.assertEqual([item["id"] for item in second["items"]], [ids[2], ids[1]])
        self.assertEqual(second["next_cursor"], ids[1])

    def test_limit_is_validated(self):
        self.login()
        for bad in ("limit=0", "limit=51"):
            self.assertEqual(
                self.client.get(f"/api/needs-you/inbox?{bad}").status_code, 422)

    def test_mark_one_read_and_404(self):
        self.login()
        row_id = self.seed(1)[0]
        marked = self.client.patch(f"/api/needs-you/inbox/{row_id}").json()
        self.assertIsNotNone(marked["read_at"])
        self.assertEqual(self.client.get("/api/needs-you/inbox/count").json(), {"count": 0})
        self.assertEqual(self.client.patch("/api/needs-you/inbox/99999").status_code, 404)


class InboxSurfaceTests(unittest.TestCase):
    """The console ships the entry point: bell button + drawer + bundle."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(
            _EmptyBackend(),
            password=_PASSWORD,
            session_secret=_SECRET,
            share_db=":memory:",
            home_db=":memory:",
            needs_you_inbox_db=str(Path(self.directory.name) / "inbox.db"),
            remote_store=_NoRemoteHosts(),
        ))

    def tearDown(self):
        self.directory.cleanup()

    def test_console_page_has_the_bell_and_the_drawer(self):
        self.client.post("/login", data={"password": _PASSWORD},
                         follow_redirects=False)
        page = self.client.get("/").text
        self.assertIn('id="inbox-button"', page)
        self.assertIn('id="inbox-badge"', page)
        self.assertIn('id="inbox-drawer"', page)
        self.assertIn('id="i-bell"', page)
        bundle = self.client.get("/assets/console.bundle.js").text
        self.assertIn("TmuxNeedsYouInbox", bundle)
        self.assertIn("/api/needs-you/inbox/count", bundle)
        self.assertIn("/api/needs-you/inbox/open", bundle)
        css = self.client.get("/assets/console.css").text
        self.assertIn(".inbox-drawer", css)
        script = self.client.get("/assets/needs-you-inbox.js")
        self.assertEqual(script.status_code, 200)

    def test_home_sidebar_links_to_the_inbox(self):
        self.client.post("/login", data={"password": _PASSWORD},
                         follow_redirects=False)
        page = self.client.get("/home").text
        self.assertIn('href="/?inbox=1"', page)


if __name__ == "__main__":
    unittest.main()
