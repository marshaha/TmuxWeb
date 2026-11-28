"""子 agent 调用图：父子边、状态、环形剪枝，以及 relay ask 真的会落一条记录。"""
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
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

    def test_only_a_unique_new_user_turn_confirms_delivery(self):
        text = "[#1 → #2] " + "long message " * 40
        call = self.store.start("%1", "%2", text, "message")
        self.store.progress(call.id, "submitted")
        message = {"id": "m", "timestamp": call.created+1, "role": "user", "text": text}
        def check(msg, child="%2"):
            return self.store.match_receipts(child, {"session_id":"s", "messages":[msg]})
        for changes in ({"role":"assistant"}, {"text":text[:280]}, {"timestamp":call.created-5},
                        {"timestamp":None}, {"text":text+" extra"}):
            self.assertEqual(check(dict(message, **changes)), {})
        self.assertEqual(check(message, "%3"), {})
        self.assertEqual(self.store.get(call.id).received_at, 0)
        self.assertEqual(check(message)["m"]["id"], call.id)
        self.assertEqual(check(dict(message, text="reused ordinal")), {})
        reader = agent_calls.AgentCallStore(self.store.path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.outbound("%1")[0]["received_at"], message["timestamp"])
        self.assertEqual(reader.pending_recipients(), {})
        second = self.store.start("%3", "%2", text, "message")
        self.store.progress(second.id, "submitted")
        self.assertEqual(check(message)["m"]["id"], call.id)
        self.assertEqual(self.store.get(second.id).received_at, 0)
        third = self.store.start("%4", "%2", text, "message")
        self.store.progress(third.id, "submitted")
        self.assertEqual(check(dict(message, id="ambiguous", timestamp=third.created+1)), {})

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

    def test_relationships_include_aliases_and_distinguish_both_directions(self):
        old = self.store.start("%1", "%2", "first")
        self.store.finish(old.id, "old answer")
        self.store.start("#3", "%2", "next")
        self.store.start("%2", "%1", "reverse")
        self.store.start("", "%1", "human")
        rows = self.store.relationships({"%1", "#3"}, 40)
        self.assertEqual([(r["relation"], r["peer"], r["question"]) for r in rows],
                         [("parent", "%2", "reverse"), ("child", "%2", "next")])

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

    def test_worker_confirms_message_before_receiver_opens_chat(self):
        text = "[#99 → #4] review"
        sent = self.client.post("/api/relay/message", json={"target":"%4","caller":"%99","text":text}).json()
        stamp = time.time()
        data = {"session_id":"s","mtime":1,"messages":[{"id":"turn","text":text,"role":"user","timestamp":stamp}]}
        with patch.object(self.tmux, "transcript", return_value=data), self.client:
            deadline = time.monotonic()+12
            while time.monotonic()<deadline:
                item = self.client.get("/api/agents/calls/"+sent["call"]).json()
                if item.get("received_at"):
                    break
                time.sleep(.1)
            self.assertEqual(item.get("received_at"), stamp)
            received = self.client.get("/api/panes/%254/transcript").json()["messages"][0]
            self.assertEqual(received["relay_receipt"], {"id":sent["call"], "child":"%4", "parent":"%99"})
            self.assertEqual(self.tmux.sent, [("%4",text,True)])

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
        self.assertIn("TmuxAgents.mount({getPane:()=>state.selected", app)
        css = self.client.get("/assets/console.css").text
        self.assertIn(".agents-panel{position:fixed", css)


if __name__ == "__main__":
    unittest.main()
