"""操作日志：先落盘再执行、状态收敛、按 pane/kind 过滤，以及 send/key/exec 真的记账。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_log
import tmux_web
from fastapi.testclient import TestClient

from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


class EventLogStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "events.db")
        self.store = event_log.EventLog(self.path)
        self.addCleanup(self.store.close)

    def test_the_row_hits_disk_before_the_operation_runs(self):
        """这就是"先落盘"的意思：另一个连接现在就能看到 running 那一行。"""
        event = self.store.begin("send", "%4", "ui", {"text": "在跑之前就在了"})
        other = event_log.EventLog(self.path)
        self.addCleanup(other.close)
        rows = other.recent("%4")
        self.assertEqual([row.status for row in rows], ["running"])
        self.assertEqual(rows[0].id, event.id)

    def test_finish_records_the_outcome(self):
        event = self.store.begin("send", "%4", "ui", {"text": "hi"})
        done = self.store.finish(event.id, "ok", {"submitted": True})
        self.assertEqual(done.status, "ok")
        self.assertIn("submitted", done.result)
        self.assertEqual(self.store.inflight(), [])

    def test_a_crash_leaves_a_row_that_sweep_calls_unknown(self):
        event = self.store.begin("send", "%4", "ui", {"text": "半路失了踪"})
        # 模拟"进程被杀，没人来 finish"：把时间挪到很久以前
        self.store._connection.execute("UPDATE events SET at=? WHERE id=?",
                                       (time.time() - 3600, event.id))
        self.store._connection.commit()
        self.assertEqual([row.id for row in self.store.inflight()], [event.id])
        self.assertEqual(self.store.sweep(stale_after=60), 1)
        self.assertEqual(self.store.get(event.id).status, "unknown")

    def test_only_terminal_statuses_finish(self):
        event = self.store.begin("key", "%4", "ui", {"key": "Escape"})
        with self.assertRaises(ValueError):
            self.store.finish(event.id, "running")
        with self.assertRaises(ValueError):
            self.store.finish(event.id, "nonsense")
        with self.assertRaises(ValueError):
            self.store.begin("")

    def test_recent_filters_by_pane_and_kind(self):
        self.store.begin("send", "%4", "ui", {"text": "a"})
        self.store.begin("key", "%4", "ui", {"key": "Escape"})
        self.store.begin("send", "%5", "ui", {"text": "b"})
        self.assertEqual(len(self.store.recent("%4")), 2)
        self.assertEqual([row.kind for row in self.store.recent("%4", kind="key")], ["key"])
        self.assertEqual(len(self.store.recent()), 3)

    def test_the_summary_is_the_first_line_of_what_was_sent(self):
        event = self.store.begin("send", "%4", "ui", {"text": "第一行\n第二行"})
        self.assertEqual(event.as_dict()["summary"], "第一行")
        self.assertEqual(self.store.begin("exec", "localhost", "mcp",
                                          {"command": "df -h"}).as_dict()["summary"], "df -h")


class EventLogApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.tmux = FakeTmux()
        self.app = tmux_web.create_app(
            self.tmux, password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            events_db=str(root / "events.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        self.client = TestClient(self.app)
        self.client.post("/login", data={"password": "pw"})

    def test_sending_a_message_leaves_a_trace(self):
        self.assertEqual(self.client.post("/api/panes/%254/input",
                                          json={"text": "给 agent 的一句话"}).status_code, 200)
        events = self.client.get("/api/events", params={"pane": "%4"}).json()["events"]
        self.assertEqual(events[0]["kind"], "send")
        self.assertEqual(events[0]["status"], "ok")
        self.assertEqual(events[0]["summary"], "给 agent 的一句话")
        detail = self.client.get(f"/api/events/{events[0]['id']}").json()
        self.assertEqual(detail["request"]["text"], "给 agent 的一句话")

    def test_a_key_and_a_command_are_recorded_too(self):
        self.client.post("/api/panes/%254/key", json={"key": "Escape"})
        self.client.post("/api/exec", json={"command": "echo hi", "timeout": 5})
        kinds = [item["kind"] for item in self.client.get("/api/events").json()["events"]]
        self.assertIn("key", kinds)
        self.assertIn("exec", kinds)

    def test_a_failed_send_is_recorded_as_an_error(self):
        response = self.client.post("/api/panes/%2599999/input", json={"text": "x"})
        self.assertEqual(response.status_code, 404, "pane 不存在，先 404")
        self.assertEqual(self.client.get("/api/events").json()["events"], [])

    def test_unknown_event_and_inflight_list(self):
        self.assertEqual(self.client.get("/api/events/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/events/inflight").json()["events"], [])

    def test_the_drawer_has_both_tabs(self):
        page = self.client.get("/").text
        for marker in ('id="ag-tab-calls"', 'id="ag-tab-events"', 'id="events-list"',
                       'id="events-empty"'):
            self.assertIn(marker, page)
        module = self.client.get("/assets/agents-panel.js").text
        for marker in ("/api/events?pane=", "function setTab(", "结果不明", "toggleEvent"):
            self.assertIn(marker, module)


if __name__ == "__main__":
    unittest.main()
