"""子 agent 调用图：父子边、状态、环形剪枝，以及 relay ask 真的会落一条记录。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_calls
import tmux_web
from fastapi.testclient import TestClient

from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


class AgentCallStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = agent_calls.AgentCallStore(str(Path(self.tmp.name) / "calls.db"))
        self.addCleanup(self.store.close)

    def test_a_finished_call_keeps_the_answer_and_the_duration(self):
        call = self.store.start("%1", "%2", "几点了？")
        self.assertEqual(call.status, "running")
        done = self.store.finish(call.id, reply="三点", status="ok")
        self.assertIsNotNone(done)
        self.assertEqual(done.status, "ok")
        self.assertEqual(done.reply, "三点")
        self.assertGreaterEqual(done.duration, 0)
        self.assertEqual(self.store.open_calls(), [])

    def test_only_terminal_statuses_can_finish_a_call(self):
        call = self.store.start("%1", "%2", "hi")
        with self.assertRaises(ValueError):
            self.store.finish(call.id, status="running")
        with self.assertRaises(ValueError):
            self.store.finish(call.id, status="whatever")

    def test_recent_sees_both_sides_of_the_edge(self):
        self.store.start("%1", "%2", "下")
        self.store.start("%3", "%1", "上")
        self.store.start("%8", "%9", "无关")
        children = [item.question for item in self.store.recent("%1")]
        self.assertEqual(sorted(children), ["上", "下"], "父边和子边都要算")

    def test_the_tree_nests_and_cuts_cycles(self):
        a = self.store.start("%1", "%2", "A 问 B")
        self.store.finish(a.id, reply="B 答", status="ok")
        b = self.store.start("%2", "%3", "B 问 C")
        self.store.finish(b.id, reply="C 答", status="ok")
        # 互相问：C 又问回 A —— 画图时必须剪掉，否则前端无限递归
        c = self.store.start("%3", "%1", "C 又问 A")
        self.store.finish(c.id, reply="A 又答", status="ok")
        tree = self.store.tree("%1")
        first = tree["children"][0]
        self.assertEqual(first["child"], "%2")
        self.assertEqual(first["children"][0]["child"], "%3")
        # 边保留（"C 又问回 A" 是事实），但不再往下递归，否则前端无限展开
        cycle = first["children"][0]["children"][0]
        self.assertEqual(cycle["child"], "%1")
        self.assertEqual(cycle["children"], [], "环在这里断掉")

    def test_full_detail_carries_the_whole_reply(self):
        long_reply = "长" * 5000
        call = self.store.start("%1", "%2", "要长回答")
        self.store.finish(call.id, reply=long_reply, status="ok")
        preview = self.store.get(call.id).as_dict()
        self.assertLessEqual(len(preview["reply_preview"]), 280)
        self.assertEqual(self.store.get(call.id).as_dict(full=True)["reply"], long_reply)


class AgentCallApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.tmux = FakeTmux()
        self.app = tmux_web.create_app(
            self.tmux, password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            calls_db=str(root / "calls.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        self.client = TestClient(self.app)
        self.login_response = self.client.post("/login", data={"password": "pw"})

    def test_an_ask_records_a_parent_child_edge(self):
        """真正的 relay ask 走完要留下一条 ok 记录，别人问的也算。"""
        self.tmux.auto_reply = "子 agent 的回答"
        response = self.client.post("/api/relay/ask", json={
            "target": "%4", "question": "看一下磁盘", "timeout": 10,
            "source": "unit-test", "caller": "%99"})
        self.assertEqual(response.status_code, 200, response.text)
        call_id = response.json()["call"]
        self.assertTrue(call_id)

        calls = self.client.get("/api/agents/calls", params={"pane": "%99"}).json()["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "ok")
        self.assertEqual(calls[0]["parent"], "%99")
        self.assertEqual(calls[0]["child"], "%4")

        detail = self.client.get(f"/api/agents/calls/{call_id}").json()
        self.assertEqual(detail["reply"], "子 agent 的回答")
        self.assertEqual(detail["question"], "看一下磁盘")

        tree = self.client.get("/api/agents/tree", params={"pane": "%99"}).json()
        self.assertEqual(tree["children"][0]["child"], "%4")

    def test_a_failed_ask_is_recorded_as_an_error_not_dropped(self):
        response = self.client.post("/api/relay/ask", json={
            "target": "#9999", "question": "hi", "timeout": 5, "caller": "%99"})
        self.assertEqual(response.status_code, 404)
        calls = self.client.get("/api/agents/calls", params={"pane": "%99"}).json()["calls"]
        self.assertEqual(calls, [], "连目标都解析不了时不该假装有一条在跑")

    def test_unknown_call_and_missing_pane(self):
        self.assertEqual(self.client.get("/api/agents/calls/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/agents/tree").status_code, 422)

    def test_the_panel_is_wired_into_the_console(self):
        page = self.client.get("/").text
        self.assertIn('id="agents-panel"', page)
        self.assertIn('id="agents-button"', page)
        self.assertIn('id="agents-close"', page)
        module = self.client.get("/assets/agents-panel.js").text
        for marker in ("/api/agents/calls?pane=", "ag-dot", "open: () => setOpen(true)",
                       "CSS.escape", "在等回复"):
            self.assertIn(marker, module)
        app = self.client.get("/assets/console-app.js").text
        self.assertIn("TmuxAgents.mount({getPane:()=>state.selected})", app)
        css = self.client.get("/assets/console.css").text
        self.assertIn(".agents-panel{position:fixed", css)


if __name__ == "__main__":
    unittest.main()
