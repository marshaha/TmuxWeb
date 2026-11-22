"""Needs-you inbox store: record / list / unread / resolve / retention.

The inbox is history, not the live card: a lighting is recorded once, the
row stays after the wait clears (only stamped resolved), and old rows are
pruned by age (30 days) then by count (200 rows).
"""
import tempfile
import time
import unittest
from pathlib import Path

from needs_you_inbox import (
    MAX_ITEMS, OPEN_TTL_HOURS, RETENTION_DAYS, NeedsYouInbox, menu_summary, sync_lighting,
    _now,
)

MENU = {"kind": "menu", "title": "Do you want to proceed?", "selected": 0,
        "options": [{"label": "Yes", "meta": "", "number": "1"},
                    {"label": "No", "meta": "", "number": "2"}],
        "toggles": []}
MENU_MOVED = dict(MENU, selected=1)


def _state(pane_id, prompt=None):
    return {"id": pane_id, "host_id": "localhost", "title": "claude",
            "needs_input": prompt is not None, "prompt": prompt}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = NeedsYouInbox(Path(self.directory.name) / "inbox.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_record_and_list_newest_first(self):
        first = self.store.record(pane_id="localhost~%4", title="claude",
                                  summary="Do you want to proceed? — 1. Yes / 2. No")
        second = self.store.record(pane_id="localhost~%5", title="kimi", summary="x")
        page = self.store.list_items()
        self.assertEqual([row["id"] for row in page["items"]], [second, first])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(self.store.unread_count(), 2)

    def test_cursor_pages_do_not_repeat_or_skip(self):
        ids = [self.store.record(pane_id=f"localhost~%{i}", summary=f"m{i}")
               for i in range(5)]
        first = self.store.list_items(limit=2)
        self.assertEqual([row["id"] for row in first["items"]], ids[::-1][:2])
        self.assertEqual(first["next_cursor"], ids[3])
        second = self.store.list_items(limit=2, before=first["next_cursor"])
        self.assertEqual([row["id"] for row in second["items"]], [ids[2], ids[1]])
        self.assertEqual(second["next_cursor"], ids[1])

    def test_open_marks_all_read_and_reports_the_count(self):
        self.store.record(pane_id="localhost~%4", summary="a")
        self.store.record(pane_id="localhost~%5", summary="b")
        self.assertEqual(self.store.mark_all_read(), 2)
        self.assertEqual(self.store.unread_count(), 0)
        # opening again marks nothing: read_at is sticky, not rewritten
        self.assertEqual(self.store.mark_all_read(), 0)

    def test_mark_one_read_404s_on_a_missing_row(self):
        row_id = self.store.record(pane_id="localhost~%4", summary="a")
        marked = self.store.mark_read(row_id)
        self.assertIsNotNone(marked["read_at"])
        self.assertEqual(self.store.unread_count(), 0)
        self.assertIsNone(self.store.mark_read(99999))

    def test_resolve_stamps_but_never_deletes(self):
        row_id = self.store.record(pane_id="localhost~%4", summary="a")
        self.assertTrue(self.store.resolve_record(row_id, "cleared"))
        self.assertFalse(self.store.resolve_record(row_id, "cleared"))
        row = self.store.list_items()["items"][0]
        self.assertIsNotNone(row["resolved_at"])
        self.assertEqual(row["resolved_reason"], "cleared")

    def test_retention_deletes_by_age_first(self):
        old = self.store.record(pane_id="localhost~%4", summary="old")
        new = self.store.record(pane_id="localhost~%5", summary="new")
        ancient = time.time() - (RETENTION_DAYS + 1) * 86400
        self.store._conn.execute(
            "UPDATE inbox SET created_at = ? WHERE id = ?", (ancient, old))
        self.store._conn.commit()
        self.assertEqual(self.store.prune(), 1)
        ids = [row["id"] for row in self.store.list_items()["items"]]
        self.assertEqual(ids, [new])

    def test_retention_caps_at_200_rows_oldest_first(self):
        ids = [self.store.record(pane_id=f"localhost~%{i}", summary=f"m{i}")
               for i in range(MAX_ITEMS + 5)]
        # the insert path already pruned: the table holds the newest 200
        self.assertEqual(self.store.unread_count(), MAX_ITEMS)
        kept = [row["id"] for row in self.store.list_items(limit=50)["items"]]
        self.assertEqual(kept, ids[5:][::-1][:50])

    def test_record_rejects_an_empty_pane(self):
        with self.assertRaises(ValueError):
            self.store.record(pane_id="   ", summary="x")


class SummaryTests(unittest.TestCase):
    def test_menu_summary_is_title_plus_labels(self):
        self.assertEqual(menu_summary(MENU),
                         "Do you want to proceed? — 1. Yes / 2. No")

    def test_menu_summary_without_a_title_is_just_options(self):
        self.assertEqual(menu_summary({"kind": "confirm", "options": [
            {"label": "Yes"}, {"label": "No"}]}), "Yes / No")

    def test_menu_summary_falls_back_to_kind(self):
        self.assertEqual(menu_summary({"kind": "menu", "options": []}), "menu")

    def test_menu_summary_never_carries_keys(self):
        prompt = dict(MENU, options=[dict(MENU["options"][0], keys=["1", "Enter"])])
        self.assertNotIn("Enter", menu_summary(prompt))


class SyncLightingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = NeedsYouInbox(Path(self.directory.name) / "inbox.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_first_lighting_records_one_row(self):
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        self.assertEqual(len(self.store.list_items()["items"]), 1)
        self.assertEqual(self.store.unread_count(), 1)
        self.assertIn("localhost~%4", lit)

    def test_the_same_menu_lit_twice_records_once(self):
        lit = {}
        states = [_state("localhost~%4", MENU)]
        sync_lighting(self.store, lit, states)
        sync_lighting(self.store, lit, states)
        sync_lighting(self.store, lit, states)
        self.assertEqual(len(self.store.list_items()["items"]), 1)

    def test_a_changed_menu_supersedes_the_open_row(self):
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU_MOVED)])
        rows = self.store.list_items()["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["resolved_reason"], "superseded")
        self.assertIsNone(rows[0]["resolved_at"])

    def test_a_cleared_wait_resolves_but_keeps_the_row(self):
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        sync_lighting(self.store, lit, [_state("localhost~%4")])
        rows = self.store.list_items()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolved_reason"], "cleared")
        self.assertEqual(self.store.unread_count(), 1)
        self.assertNotIn("localhost~%4", lit)

    def test_a_restart_files_away_rows_that_were_still_open(self):
        """重启后没人能再清这些行：必须自己归档，否则铃铛永远亮着。"""
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU), _state("localhost~%5", MENU)])
        self.assertEqual(self.store.open_count(), 2)
        self.assertEqual(self.store.resolve_open("restarted"), 2)
        self.assertEqual(self.store.open_count(), 0)
        rows = self.store.list_items()["items"]
        self.assertEqual({row["resolved_reason"] for row in rows}, {"restarted"})

    def test_a_menu_left_open_for_half_a_day_is_expired(self):
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        old = _now() - (OPEN_TTL_HOURS + 1) * 3600
        self.store._conn.execute("UPDATE inbox SET created_at = ?", (old,))
        self.store._conn.commit()
        self.store.prune()
        self.assertEqual(self.store.open_count(), 0)
        self.assertEqual(self.store.list_items()["items"][0]["resolved_reason"], "expired")

    def test_the_list_can_show_only_what_is_still_open(self):
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        sync_lighting(self.store, lit, [_state("localhost~%4")])
        sync_lighting(self.store, lit, [_state("localhost~%5", MENU)])
        self.assertEqual(len(self.store.list_items()["items"]), 2)
        open_rows = self.store.list_items(open_only=True)["items"]
        self.assertEqual([row["pane_id"] for row in open_rows], ["localhost~%5"])

    def test_a_vanished_pane_is_left_alone(self):
        # Gone from the sweep can mean its host is unreachable, not that it
        # closed: no resolve, and no duplicate if it comes back lit.
        lit = {}
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        sync_lighting(self.store, lit, [])
        sync_lighting(self.store, lit, [_state("localhost~%4", MENU)])
        self.assertEqual(len(self.store.list_items()["items"]), 1)


class InboxApiTests(unittest.TestCase):
    """铃铛上的数字 = 真的在等；重启不许留坟场。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _app(self):
        import tmux_web
        from fastapi.testclient import TestClient
        from tests.test_tmux_web import (FakeRemoteStore, FakeSSHRunner,
                                         FakeTerminalFactory, FakeTmux)
        app = tmux_web.create_app(
            FakeTmux(), password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(self.root / "shares.db"),
            needs_you_inbox_db=str(self.root / "inbox.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        client = TestClient(app)
        client.post("/login", data={"password": "pw"})
        return client

    def test_a_new_process_files_away_rows_the_old_one_left_open(self):
        store = NeedsYouInbox(self.root / "inbox.db")
        sync_lighting(store, {}, [_state("localhost~%4", MENU), _state("localhost~%5", MENU)])
        self.assertEqual(store.open_count(), 2)
        store.close()

        client = self._app()                      # 相当于服务重启
        self.assertEqual(client.get("/api/needs-you/inbox/count").json()["count"], 0)
        self.assertEqual(client.get("/api/needs-you/inbox").json()["items"], [])

    def test_the_count_is_open_rows_not_unread_history(self):
        store = NeedsYouInbox(self.root / "inbox.db")
        lit = {}
        sync_lighting(store, lit, [_state("localhost~%4", MENU)])
        sync_lighting(store, lit, [_state("localhost~%4")])     # 菜单消失 → cleared
        store.close()

        client = self._app()
        payload = client.get("/api/needs-you/inbox/count").json()
        self.assertEqual(payload["count"], 0, "清了的历史不该继续点亮铃铛")
        self.assertEqual(payload["unread"], 1)
        self.assertEqual(len(client.get("/api/needs-you/inbox").json()["items"]), 0)
        history = client.get("/api/needs-you/inbox", params={"history": 1}).json()["items"]
        self.assertEqual(len(history), 1, "历史还在，只是不默认展示")


if __name__ == "__main__":
    unittest.main()
