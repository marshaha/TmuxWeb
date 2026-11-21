"""记住的授权：作用域、过期、指纹不匹配 —— 自动答题只能在这道题上生效。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import approvals
import tmux_web
from fastapi.testclient import TestClient

from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = approvals.ApprovalStore(str(Path(self.tmp.name) / "a.db"))
        self.addCleanup(self.store.close)

    def test_a_turn_grant_only_covers_its_own_turn(self):
        self.store.remember("%4", "sig-1", {"index": 0}, scope="turn", turn="t1")
        self.assertIsNotNone(self.store.find("%4", "sig-1", "t1"))
        self.assertIsNone(self.store.find("%4", "sig-1", "t2"),
                          "新的一轮要重新问，不能凭上一轮的授权继续答")

    def test_an_always_grant_covers_any_turn(self):
        self.store.remember("%4", "sig-1", {"index": 0}, scope="always", turn="t1")
        self.assertIsNotNone(self.store.find("%4", "sig-1", "something-else"))

    def test_a_different_menu_never_matches(self):
        self.store.remember("%4", "sig-allow", {"index": 0}, scope="always", turn="t1")
        self.assertIsNone(self.store.find("%4", "sig-delete-database", "t1"))

    def test_a_menu_without_a_signature_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.remember("%4", "", {"index": 0})
        with self.assertRaises(ValueError):
            self.store.remember("%4", "sig", {"index": 0}, scope="forever")

    def test_re_remembering_replaces_the_old_answer(self):
        first = self.store.remember("%4", "sig-1", {"index": 0}, scope="always")
        second = self.store.remember("%4", "sig-1", {"index": 1}, scope="always")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.store.list("%4")), 1, "同一道题只留最新那条")
        found = self.store.find("%4", "sig-1")
        self.assertIsNotNone(found)
        self.assertEqual(found.action, '{"index": 1}')

    def test_an_expired_grant_stops_working(self):
        grant = self.store.remember("%4", "sig-1", {"index": 0}, scope="always")
        self.store._connection.execute("UPDATE approvals SET expires=? WHERE id=?",
                                       (time.time() - 1, grant.id))
        self.store._connection.commit()
        self.assertIsNone(self.store.find("%4", "sig-1"))
        self.assertEqual(self.store.purge(), 1)

    def test_uses_are_counted_and_forget_works(self):
        grant = self.store.remember("%4", "sig-1", {"index": 0}, scope="turn", turn="t1")
        self.store.mark_used(grant.id)
        self.store.mark_used(grant.id)
        self.assertEqual(self.store.list("%4")[0].uses, 2)
        self.assertTrue(self.store.forget(grant.id))
        self.assertFalse(self.store.forget(grant.id))
        self.assertEqual(self.store.list("%4"), [])


class ApprovalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.tmux = FakeTmux()
        self.app = tmux_web.create_app(
            self.tmux, password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            approvals_db=str(root / "approvals.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        self.client = TestClient(self.app)
        self.client.post("/login", data={"password": "pw"})

    def test_remembering_and_revoking_over_http(self):
        created = self.client.post("/api/panes/%254/approvals",
                                   json={"signature": "sig-9", "action": {"index": 1},
                                         "scope": "turn", "turn": "t7", "label": "允许"})
        self.assertEqual(created.status_code, 200, created.text)
        grant = created.json()
        listed = self.client.get("/api/panes/%254/approvals", params={"turn": "t7"}).json()
        self.assertEqual([item["id"] for item in listed["items"]], [grant["id"]])
        other = self.client.get("/api/panes/%254/approvals", params={"turn": "t8"}).json()
        self.assertEqual(other["items"], [], "别的轮次看不到这条授权")

        used = self.client.post(f"/api/panes/%254/approvals/{grant['id']}/used")
        self.assertTrue(used.json()["counted"])
        self.assertEqual(self.client.get("/api/panes/%254/approvals").json()["items"][0]["uses"], 1)

        self.assertEqual(self.client.delete(f"/api/panes/%254/approvals/{grant['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/panes/%254/approvals/{grant['id']}").status_code, 404)

    def test_an_approval_without_a_signature_is_refused(self):
        response = self.client.post("/api/panes/%254/approvals", json={"action": {"index": 0}})
        self.assertEqual(response.status_code, 422)
        self.assertIn("指纹", response.text)
        bad = self.client.post("/api/panes/%254/approvals",
                               json={"signature": "s", "action": {"index": 0}, "scope": "forever"})
        self.assertEqual(bad.status_code, 422)


class AutoAnswerWiringTests(unittest.TestCase):
    """自动答题必须由"记住"开关触发，且只认同一道题的指纹。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app = tmux_web.create_app(
            FakeTmux(), password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            approvals_db=str(root / "approvals.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        self.client = TestClient(self.app)
        self.client.post("/login", data={"password": "pw"})

    def test_the_card_asks_before_it_remembers_anything(self):
        page = self.client.get("/assets/prompt-card.js").text
        self.assertIn("if (!rememberChoice || !lastSignature) return false;", page)
        self.assertIn("rememberScope = 'turn'", page, "默认只记本轮，不是一直")
        self.assertIn("data.grant && autoAnswered !== data.signature", page)
        self.assertIn("/approvals/${encodeURIComponent(data.grant.id)}/used", page)
        self.assertIn("prompt-remember", self.client.get("/").text)
        # 签名在响应里（不在 prompt 对象里），漏传就等于永远记不住 —— 这里钉死
        self.assertIn("render(data.prompt, data.screen, data.signature)", page)
        # poll 自己就持有 busy，用它判断"手里还有一条"会永远为真
        self.assertIn("if (answering) return;", page)
        self.assertIn("已按你的授权自动选了", page)
        self.assertIn("撤销", page)

    def test_the_menu_signature_covers_title_options_and_toggles(self):
        base = {"title": "允许编辑?", "options": [{"label": "是"}, {"label": "否"}]}
        self.assertEqual(tmux_web.agent_prompts.menu_signature(base),
                         tmux_web.agent_prompts.menu_signature(dict(base)))
        changed = {"title": "允许编辑?", "options": [{"label": "是"}, {"label": "否，以后别问"}]}
        self.assertNotEqual(tmux_web.agent_prompts.menu_signature(base),
                            tmux_web.agent_prompts.menu_signature(changed))
        self.assertEqual(tmux_web.agent_prompts.menu_signature({}), "")
