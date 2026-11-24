import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import hermes_forms as forms


QUESTIONS = [
    {"qid": "provider", "question": "选择 provider？", "choices": ["openai", "claude"], "multi_select": False},
    {"qid": "model", "question": "选择模型？", "choices": ["默认模型", "其他模型"], "multi_select": False},
]
NOTICE = ("fixture-session", "fixture-hook", "需要回答问题", "\n".join(q["question"] for q in QUESTIONS))
ANSWERS = {"provider": {"indices": [1]}, "model": {"indices": [0]}}


class Owner:
    """One native outstanding request, settled by one whole JSON-RPC response."""
    def __init__(self):
        self.open = [{"id": "srq-one", "method": "clarify", "params": {"questions": copy.deepcopy(QUESTIONS)}}]
        self.prompts = []
        self.answers = []
        self.websocket = self

    async def rpc(self, method, **args):
        if method == "session.active_list":
            return {"sessions":[{"id":NOTICE[0],"status":"waiting" if self.prompts else "idle"}]}
        if method == "approval.respond":
            assert args["execution_generation"] == 3 and args["prompt_id"] == "approval-1"
            assert args["choice"] in ("once", "deny")
            self.answers.append(args["choice"])
            self.prompts = []
            return {"status": "resolved"}
        assert method in ("session.events.since", "session.resume")
        assert args["session_id"] == NOTICE[0]
        return {"open_requests": copy.deepcopy(self.open), "prompts": self.prompts,
                "execution_generation": 3, "replay_epoch": "e", "last_sequence": 4,
                "events": [] if self.open else [{"type": "request.cancel",
                    "payload": {"id": "srq-one", "reason": "resolved"}}]}

    async def send(self, raw):
        frame = json.loads(raw)
        assert frame["jsonrpc"] == "2.0"
        if self.open and frame["id"] == self.open[0]["id"]:
            self.answers.append(frame["result"])
            self.open = []


async def exchange(owner, submission=None, notice=NOTICE, live=True):
    return await forms.exchange(owner, notice, "%4", "1201", submission, lambda: live)


class NativeFormsTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_submit_is_one_native_answer(self):
        owner = Owner()
        form = (await exchange(owner))["form"]
        action = {"revision": form["revision"], "answers": ANSWERS}
        first, second = await asyncio.gather(exchange(owner, action), exchange(owner, action))
        self.assertEqual((first["status"], second["status"]), ("resolved", "closed"))
        self.assertEqual(owner.answers, [{"outcome": "submitted", "answers": {"provider": "claude", "model": "默认模型"}}])

    async def test_receipt_survives_process_and_rejects_conflicting_submission(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        owner = Owner()
        revision = (await exchange(owner))["form"]["revision"]
        action = {"revision":revision, "answers":ANSWERS}
        with TemporaryDirectory() as temp:
            def receipt(a):
                return forms.Receipts(Path(temp), "%4", "1201", a)
            await forms.exchange(owner, NOTICE, "%4", "1201", action, lambda: True, receipt(action))
            self.assertEqual(receipt(action).access("lookup"), {"status":"resolved"})
            different = {"revision":revision, "answers":{**ANSWERS,"model":{"indices":[1]}}}
            self.assertEqual(receipt(different).access("lookup"), {"status":"closed"})
            self.assertEqual(len(owner.answers), 1)

    async def test_stale_requests_and_foreign_panes(self):
        owner = Owner()
        rev = (await exchange(owner))["form"]["revision"]
        submit = {"revision": rev, "answers": ANSWERS}
        self.assertEqual((await exchange(owner, submit, live=False))["status"], "closed")
        owner.open[0]["id"] = "srq-new"
        self.assertEqual((await exchange(owner, submit))["status"], "closed")
        owner.open[0]["params"]["answers"] = {"model": "a terminal answer"}
        self.assertEqual((await exchange(owner))["status"], "closed")
        self.assertEqual(owner.answers, [])

    async def test_approval_is_once_or_deny_only(self):
        owner = Owner()
        owner.open = []
        owner.prompts = [{"kind": "approval", "prompt_id": "approval-1", "execution_generation": 3,
                          "command": "example", "choices": ["once", "session", "always", "deny"]}]
        notice = (*NOTICE[:2], "等待操作审批", "")
        form = (await exchange(owner, notice=notice))["form"]
        self.assertEqual(len(form["questions"][0]["choices"]), 2)
        for choice in ({"indices":[2]}, {"indices":[], "other":"always"}):
            self.assertEqual((await exchange(owner, {"revision":form["revision"], "answers":{"decision":choice}},
                                             notice=notice))["status"], "invalid")
        await exchange(owner, {"revision":form["revision"], "answers":{"decision":{"indices":[1]}}}, notice=notice)
        self.assertEqual(owner.answers, ["deny"])

    async def test_ambiguous_secret_or_history_never_opens(self):
        owner = Owner()
        owner.open.append(copy.deepcopy(owner.open[0]))
        self.assertEqual((await exchange(owner))["status"], "closed")
        owner.open.pop()
        owner.open[0]["method"] = "sudo"
        self.assertEqual((await exchange(owner))["status"], "closed")
        owner.open = []
        self.assertEqual((await exchange(owner))["status"], "closed")

    async def test_other_multi_and_incomplete(self):
        owner = Owner()
        owner.open[0]["params"]["questions"][1]["multi_select"] = True
        form = (await exchange(owner))["form"]
        submit = {"revision":form["revision"],"answers":{"provider":{"indices":[True]}}}
        self.assertEqual((await exchange(owner,submit))["status"], "invalid")
        submit["answers"] = {"provider":{"indices":[], "other":"我的选择"}, "model":{"indices":[1], "other":"自定义"}}
        await exchange(owner,submit)
        self.assertEqual(json.loads(owner.answers[0]["answers"]["model"]), ["其他模型","自定义"])
        self.assertEqual(owner.answers[0]["answers"]["provider"], "我的选择")

    def test_no_live_terminal_never_uses_local_history(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as home, patch.object(forms, "terminal_session", return_value=None):
            self.assertIsNone(forms.binding("%4","123",Path(home)))

    def test_post_requires_login_and_federation_input_grant(self):
        from tests.test_tmux_web import TmuxWebTests
        fixture = TmuxWebTests()
        fixture.setUp()
        try:
            with fixture.client as client, patch.object(forms, "on_host") as helper:
                action = {"revision": "a"*64, "answers": ANSWERS}
                self.assertEqual(client.post("/api/panes/%254/native-form", json=action).status_code, 401)
                self.assertIn(client.post("/api/fed/panes/%254/native-form", json=action).status_code, (401,403))
                fixture.login()
                bad = {"revision": "a"*64, "answers":{"provider":{"indices":[True]}}}
                self.assertEqual(client.post("/api/panes/%254/native-form", json=bad).status_code, 422)
                helper.assert_not_called()
        finally:
            fixture.tearDown()
