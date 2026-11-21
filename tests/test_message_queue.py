"""持久排队：存储语义 + worker（空闲才发、忙就等、steer 先中断）。"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import message_queue
import tmux_web
from fastapi.testclient import TestClient

from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = message_queue.QueueStore(str(Path(self.tmp.name) / "q.db"))
        self.addCleanup(self.store.close)

    def test_items_survive_a_reopen(self):
        item = self.store.add("%4", "边跑边排队", submit=True, mode="queue")
        reopened = message_queue.QueueStore(self.store.path)
        self.addCleanup(reopened.close)
        self.assertEqual([entry.text for entry in reopened.list("%4")], ["边跑边排队"])
        self.assertEqual(reopened.get(item.id).state, "pending")

    def test_claim_marks_sending_and_finish_sends_it(self):
        item = self.store.add("%4", "hello")
        claimed = self.store.claim_next("%4")
        self.assertEqual(claimed.id, item.id)
        self.assertEqual(claimed.state, "sending")
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNone(self.store.claim_next("%4"), "同一条不能被领两次")
        self.store.finish(item.id, "sent")
        self.assertEqual(self.store.list("%4"), [], "发完就从待发列表里消失")
        self.assertEqual(self.store.list("%4", include_done=True)[0].state, "sent")

    def test_cancel_only_touches_pending_or_sending(self):
        item = self.store.add("%4", "x")
        self.assertTrue(self.store.cancel(item.id))
        self.assertFalse(self.store.cancel(item.id), "已经取消过的不再动")
        done = self.store.add("%4", "y")
        self.store.claim_next("%4")
        self.store.finish(done.id, "sent")
        self.assertFalse(self.store.cancel(done.id))

    def test_restart_puts_inflight_items_back(self):
        item = self.store.add("%4", "半路挂了的")
        self.store.claim_next("%4")
        self.assertEqual(self.store.reset_inflight(), 1)
        self.assertEqual(self.store.get(item.id).state, "pending")

    def test_attempts_are_capped_then_it_fails(self):
        item = self.store.add("%4", "一直发不出去")
        for _ in range(message_queue.MAX_ATTEMPTS):
            claimed = self.store.claim_next("%4")
            self.assertIsNotNone(claimed)
            self.store.retry_later(claimed.id, "boom")
        self.assertIsNone(self.store.claim_next("%4"), "用尽尝试次数就不该再被领")
        self.assertEqual(self.store.get(item.id).state, "failed")
        self.assertIn("boom", self.store.get(item.id).error)

    def test_empty_text_and_unknown_mode_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add("%4", "   ")
        with self.assertRaises(ValueError):
            self.store.add("%4", "x", mode="teleport")


class QueueWorkerTests(unittest.TestCase):
    """真的把 app 跑起来（startup 会拉起 worker），看它有没有按时把消息送出去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmux = FakeTmux()
        self.app = tmux_web.create_app(
            self.tmux, password="unit-test-admin-password", session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:",
            queue_db=str(Path(self.tmp.name) / "queue.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner)

    def start(self):
        client = TestClient(self.app)
        client.__enter__()                       # startup: 重置 sending + 起 worker
        self.addCleanup(lambda: client.__exit__(None, None, None))
        client.post("/login", data={"password": "unit-test-admin-password"},
                    follow_redirects=False)
        return client

    def wait_for(self, predicate, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_an_idle_pane_gets_the_queued_message(self):
        client = self.start()
        response = client.post("/api/panes/%254/queue", json={"text": "排队的一条"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending"], 1)
        self.assertTrue(self.wait_for(lambda: self.tmux.sent and self.tmux.sent[-1][1] == "排队的一条"),
                        self.tmux.sent)
        self.assertTrue(self.wait_for(
            lambda: client.get("/api/panes/%254/queue").json()["items"] == []))

    def test_a_busy_pane_holds_the_message_until_it_is_done(self):
        client = self.start()
        busy = mock.patch.object(tmux_web.agent_status, "status_for_pane",
                                 return_value=tmux_web.agent_status.BUSY)
        with busy:
            client.post("/api/panes/%254/queue", json={"text": "等它跑完"})
            time.sleep(3.2)                      # worker 至少扫过一轮
            self.assertEqual([text for _pane, text, _submit in self.tmux.sent], [],
                             "忙的时候不能往里塞")
            items = client.get("/api/panes/%254/queue").json()["items"]
            self.assertEqual([item["state"] for item in items], ["pending"])
        self.assertTrue(self.wait_for(lambda: self.tmux.sent and self.tmux.sent[-1][1] == "等它跑完"),
                        self.tmux.sent)

    def test_steer_interrupts_first(self):
        client = self.start()
        client.post("/api/panes/%254/queue", json={"text": "别干了，改成这个", "mode": "steer"})
        self.assertTrue(self.wait_for(lambda: bool(self.tmux.sent)), self.tmux.sent)
        self.assertTrue(self.tmux.keys, "steer 必须先按中断键")
        self.assertIn("Escape", [key for _pane, key in self.tmux.keys])

    def test_the_api_refuses_empty_text_and_bad_mode(self):
        client = self.start()
        self.assertEqual(client.post("/api/panes/%254/queue", json={"text": "  "}).status_code, 422)
        self.assertEqual(client.post("/api/panes/%254/queue",
                                     json={"text": "x", "mode": "teleport"}).status_code, 422)

    def test_cancelling_removes_it_from_the_queue(self):
        client = self.start()
        item_id = client.post("/api/panes/%254/queue", json={"text": "算了不发了"}).json()["item"]["id"]
        self.assertEqual(client.delete(f"/api/panes/%254/queue/{item_id}").status_code, 200)
        self.assertEqual(client.get("/api/panes/%254/queue").json()["items"], [])
        self.assertEqual(client.delete(f"/api/panes/%254/queue/{item_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
