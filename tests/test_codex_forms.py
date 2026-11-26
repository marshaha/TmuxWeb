import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import codex_forms


class Owner:
    def __init__(self, home):
        self.home = home
        self.thread = "fixture-thread"
        self.name = "Color test"
        self.folder = home / ".codex/sessions"
        self.folder.mkdir(parents=True)
        (self.folder.parent / "session_index.jsonl").write_text(json.dumps({"id":self.thread,"thread_name":self.name}))
        self.path = self.folder / ("rollout-" + self.thread + ".jsonl")
        self.item = {"id":"question-id","type":"AgentMessage","delivery":"async","questions":[
            {"title":"Which color?", "options":["Blue","Green","Orange"]}]}
        self.path.write_text(json.dumps({"type":"event_msg","payload":{"type":"item_completed","turn_id":"turn","item":self.item}}))
        self.screen = ("• Queued follow-up inputs\n? 1 question\nshift+← to answer\n"
                       "› Ask Codex to do anything\nGPT · ~/workspace · Color test\n← for agents · ? for shortcuts")
        self.sent = []
        self.turn = "turn"

    async def rpc(self, method, args):
        if method == "thread/read":
            return {"thread":{"id":self.thread,"name":self.name,"path":str(self.path),"cwd":"/repo","status":{"type":"active"}}}
        if method == "thread/turns/list":
            return {"data":[{"id":self.turn,"status":"inProgress"}]}
        assert method == "turn/steer"
        self.sent.append(args)
        return {"turnId":self.turn}

    async def form(self, submission=None):
        return await codex_forms.exchange(self.home,"%4","135",lambda:(self.screen,self.name,"/repo"),submission,self.rpc)


MCP_TITLE = 'Allow the hub MCP server to run tool "tmuxweb_ask_agent"?'
MCP_SCREEN = (MCP_TITLE + "\n  timeout: 120\n"
              "  1. Allow                    Run the tool and\ncontinue\n"
              "› 2. Allow for this session   Run the tool and\nremember this choice for this session\n"
              "  3. Always allow             Run the tool and\nremember this choice for future tool calls\n"
              "  4. Cancel                   Cancel this tool\ncall\n"
              "  enter to submit | esc to cancel")


def permission_request(owner):
    return {"id": 1, "method": "mcpServer/elicitation/request", "params": {
        "threadId": owner.thread, "turnId": owner.turn, "serverName": "hub", "mode": "form",
        "message": MCP_TITLE, "requestedSchema": {"type": "object", "properties": {}},
        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"target": "%28"},
                  "persist": ["session", "always"]}}}


class CodexFormTests(unittest.TestCase):
    def test_draft_submit_once_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as work:
            owner = Owner(Path(work))
            form = asyncio.run(owner.form())["form"]
            self.assertEqual(form["questions"][0]["choices"], ["Blue","Green","Orange"])
            post = {"revision":form["revision"],"answers":{"0":{"indices":[1]}}}
            self.assertEqual(asyncio.run(owner.form(dict(post,answers={})))["status"],"invalid")
            self.assertEqual(asyncio.run(owner.form(dict(post,revision="old")))["status"],"closed")
            self.assertFalse(owner.sent)
            for _ in range(2):
                self.assertEqual(asyncio.run(owner.form(post))["status"],"resolved")
            self.assertEqual(len(owner.sent),1)
            self.assertEqual(owner.sent[0]["expectedTurnId"],"turn")
            self.assertIn("Which color?\nGreen",owner.sent[0]["input"][0]["text"])
            owner.turn = "new"
            self.assertEqual(asyncio.run(owner.form(post))["status"],"closed")
            owner.turn = "turn"
            owner.screen = owner.screen.replace(" · Color test"," · Another thread")
            self.assertEqual(asyncio.run(owner.form())["status"],"closed")
            self.assertEqual(len(owner.sent),1)

    def test_mcp_permission_requires_screen_and_current_native_request(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as work:
            home = Path(work)
            owner = Owner(home)
            req = permission_request(owner)
            chan = AsyncMock()
            chan.answer.return_value = "resolved"
            menu = codex_forms.codex_tool_menu(MCP_SCREEN)
            self.assertEqual(menu, ("Allow", "Allow for this session", "Always allow", "Cancel"))
            def form(post=None):
                return asyncio.run(codex_forms.tool_approval(home, "%4", "135",
                    lambda: (owner.screen, owner.name, "/repo"), post, owner.thread, owner.name,
                    "/repo", owner.turn, menu, req, chan))
            owner.screen = MCP_SCREEN
            card = form()["form"]
            post = {"revision": card["revision"], "answers": {"decision": {"indices": [2]}}}
            for key, bad in (("threadId", "sibling"), ("turnId", "previous"), ("mode", "url"),
                             ("_meta", {}), ("message", "another tool"),
                             ("requestedSchema", {"type":"object","properties":{"name":{}}})):
                old = req["params"][key]
                req["params"][key] = bad
                self.assertEqual(form(post)["status"], "closed")
                req["params"][key] = old
            req["id"] = 2
            self.assertEqual(form(post)["status"], "closed")
            req["id"] = 1
            owner.screen += "\n› New input"
            self.assertEqual(form(post)["status"], "closed")
            owner.screen = MCP_SCREEN
            self.assertEqual(form(dict(post, answers={}))["status"], "invalid")
            chan.answer.assert_not_awaited()
            self.assertEqual(form(post)["status"], "resolved")
            self.assertEqual(form(post)["status"], "resolved")
            chan.answer.assert_awaited_once_with(owner.thread, 1, {"action":"accept","content":None,
                                                                  "_meta":{"persist":"always"}})

    def test_new_user_message_clears_previous_question(self):
        with tempfile.TemporaryDirectory() as work:
            owner = Owner(Path(work))
            with owner.path.open('a') as stream:
                stream.write('\n' + json.dumps({"type":"response_item","payload":{"type":"message","role":"user"}}))
            self.assertEqual(asyncio.run(owner.form())["status"],"closed")
