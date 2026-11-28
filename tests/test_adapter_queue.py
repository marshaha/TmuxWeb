import json
import asyncio
import copy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

import claude_chat
import codex_forms
import hermes_forms
from tests.test_native_forms import Owner as HermesOwner, NOTICE
from tests.test_codex_forms import Owner as CodexOwner, command_request


def permission(rid, tool, args):
    return {"type": "control_request", "request_id": rid, "request": {"subtype": "can_use_tool",
            "tool_name": tool, "input": args, "tool_use_id": "tool-" + rid, "agent_id": "sub-A"}}


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_explicit_question_plan_cancel_and_duplicate_submit(self):
        owner = claude_chat.Owner()
        writes = []
        questions = {"questions": [{"question": "Models?", "multiSelect": True,
                                     "options": [{"label": "A"}, {"label": "B", "description": "second"}]}]}
        owner.receive(permission("q", "AskUserQuestion", questions), writes.append)
        owner.receive(permission("p", "ExitPlanMode", {"plan": "only edit a.txt"}), writes.append)
        owner.receive(permission("b", "Bash", {"command": "echo fixture"}), writes.append)
        owner.receive({"type": "result", "session_id": owner.session}, writes.append)
        q, plan, cmd = owner.handle(None, writes.append)["forms"]
        self.assertIn("sub-A", q["questions"][0]["description"])
        self.assertIn("only edit a.txt", plan["questions"][0]["description"])
        owner.receive({"type": "control_cancel_request", "request_id": "b"}, writes.append)
        def answer(form, answers):
            return owner.handle({"revision": form["revision"], "answers": answers}, writes.append)["status"]
        self.assertEqual(answer(cmd, {"decision": {"indices": [0]}}), "closed")
        self.assertFalse(writes)
        self.assertEqual(answer(plan, {"decision": {"indices": [1]}}), "resolved")
        self.assertEqual(writes[-1]["response"]["response"]["behavior"], "deny")
        for _ in range(2):
            self.assertEqual(answer(q, {"0": {"indices": [0,1]}}), "resolved")
        self.assertEqual(answer(q, {"0": {"indices": [0]}}), "closed")
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1]["response"]["response"]["updatedInput"],
                         dict(questions, answers={"Models?": "A, B"}))

    async def test_hermes_cursor_skips_replay_preserves_outstanding_and_resets_epoch(self):
        owner = HermesOwner()
        owner.open.append(dict(copy.deepcopy(owner.open[0]), id="sibling"))
        replay = []
        epoch = ["one"]
        gateway = owner.rpc
        async def rpc(method, **args):
            result = await gateway(method, **args)
            if method == "session.events.since":
                result.pop("replay_epoch")
                result.pop("last_sequence")
                replay.append(args["last_seen"])
                result.update(epoch=epoch[0], latest_seq=1000, events=[{}] * (1000 - args["last_seen"]))
            return result
        owner.rpc = rpc
        cursor = {}
        async def read():
            result = await hermes_forms.exchange(owner, NOTICE, "%4", "1201", None, lambda: True, cursor=cursor)
            return [(f["request_id"], f["revision"]) for f in result["forms"]]
        first = await read()
        self.assertEqual(await read(), first)
        self.assertEqual(replay, [0, 1000])
        self.assertEqual(len(first), 2)
        owner.open.pop(0)
        epoch[0] = "two"
        second = await read()
        self.assertEqual(second[0][0], first[1][0])
        self.assertNotEqual(second[0][1], first[1][1])
        self.assertEqual(replay[-2:], [1000, 0])

    async def test_hermes_mix_of_live_wire_question_and_approval(self):
        owner = HermesOwner()
        calls = []
        seen = []
        owner.open.append({"id": "approve-2", "method": "approval", "params": {
            "session_id": NOTICE[0], "request_id": "queue-key", "command": "echo example",
            "choices": ["once", "deny", "always"]}})
        async def rpc(method, **args):
            self.assertEqual(method, "session.events.since")
            calls.append(args["last_seen"])
            return {"epoch": "e", "latest_seq": 4 + len(seen), "open_requests": copy.deepcopy(owner.open),
                    "events": [{"type":"request.cancel", "payload":{"id":"approve-2", "reason":"resolved"}}] if seen else []}
        async def send(raw):
            seen.append(json.loads(raw))
            owner.open.pop()
        owner.rpc, owner.send = rpc, send
        cursor = {}
        async def read(action=None):
            return await hermes_forms.exchange(owner, (NOTICE[0],"tty:4","live",""), "%4", "123", action,
                                               lambda: True, cursor=cursor)
        question, approval = (await read())["forms"]
        self.assertEqual((await read({"revision":approval["revision"],"answers":{"decision":{"indices":[1]}}}))["status"], "resolved")
        self.assertEqual(seen, [{"jsonrpc":"2.0","id":"approve-2","result":{"choice":"deny"}}])
        self.assertEqual([f["revision"] for f in (await read())["forms"]], [question["revision"]])
        self.assertEqual(calls, [0,4,4,5])

    async def test_canonical_cursor_sends_sequence_and_epoch(self):
        owner = HermesOwner()
        cursor = {}
        await hermes_forms.event_page(owner, NOTICE[0], cursor)
        rpc = AsyncMock(return_value={"replay_epoch":"e", "last_sequence":5})
        owner.rpc = rpc
        await hermes_forms.event_page(owner, NOTICE[0], cursor)
        rpc.assert_awaited_once_with("session.events.since", session_id=NOTICE[0], last_sequence=4, replay_epoch="e")
        self.assertEqual(cursor["seq"], 5)

    def test_rollout_patch_requires_exact_turn_call_and_literal_command(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            owner = CodexOwner(home)
            patch_text = "*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch"
            command = "apply_patch <<'PATCH'\n" + patch_text + "\nPATCH"
            def read(cmd, turn="turn", call="call", extra=()):
                rows = [{"type":"turn_context", "payload":{"turn_id":turn}},
                        {"type":"response_item", "payload":{"type":"function_call", "name":"exec_command",
                         "call_id":call, "arguments":json.dumps({"cmd":cmd})}}, *extra]
                owner.path.write_text("\n".join(map(json.dumps, rows)))
                return codex_forms.rollout_patches(home, owner.path, owner.thread, "turn", {"call"}, "/repo")
            self.assertEqual(read(command)["call"], (patch_text, command))
            self.assertFalse(read(command, turn="earlier"))
            self.assertFalse(read(command, call="different"))
            self.assertFalse(read(command + "\necho unrelated"))
            self.assertFalse(read(command, extra=[{"type":"event_msg","payload":{"type":"task_complete"}}]))
            self.assertFalse(read(command.replace("'PATCH'", "PATCH")))

    async def test_codex_bound_panes_scrolled_concurrent_revisions_and_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            owner = CodexOwner(home)
            path = home/".config/tmuxweb-agent/interactions.sqlite3"
            path.parent.mkdir(parents=True)
            file_req = {"id": 6, "method": "item/fileChange/requestApproval", "params": {
                "threadId": owner.thread, "turnId": owner.turn, "itemId": "edit"}}
            command = command_request(owner)
            digest = hashlib.sha256(b"python3 check.py").hexdigest()
            channel = AsyncMock()
            channel.pending = {6: file_req, 5: command}
            channel.answer.return_value = "resolved"
            changes = [{"path": "a.txt", "kind": {"type": "update", "move_path": "new.txt"}, "diff": "+a"},
                       {"path": "b.txt", "kind": {"type": "add"}, "diff": "+b"}]
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE waits(pane,pid,provider,session,request,title,detail,at)")
                for pane, pid, session in (("%4","1201",owner.thread), ("%5","1202","other")):
                    for item, detail in (("edit","apply_patch"), ("item","command-sha256:" + digest)):
                        db.execute("INSERT INTO waits VALUES(?,?,'codex',?,?,'等待工具审批',?,?)",
                                   (pane, pid, session, item, detail, time.time()))
            async def rpc(method, args):
                if method == "thread/resume":
                    return {"thread": {"id": owner.thread, "cwd": "/repo"}}
                if method == "thread/turns/list":
                    self.assertEqual(args["itemsView"], "full" if 6 in channel.pending else "notLoaded")
                    return {"data": [{"id": owner.turn, "status": "inProgress",
                                      "items": [{"id": "edit", "type": "fileChange", "changes": changes}]}]}
                return await owner.rpc(method, args)
            async def read(pane="%4", pid="1201", action=None):
                return await codex_forms.exchange(home, pane, pid, lambda: ("scrolled", "shared title", "/repo"),
                                                  action, rpc, channel)
            f, c = (await read())["forms"]
            detail = f["questions"][0]["description"]
            self.assertLess(detail.index("b.txt"), detail.index("+a"))
            self.assertIn("new.txt", detail)
            action = {"revision": f["revision"], "answers": {"decision": {"indices": [0]}}}
            self.assertEqual((await read("%5", "1202", action))["status"], "closed")
            changes[1]["diff"] = "+different"
            self.assertEqual((await read(action=action))["status"], "closed")
            self.assertFalse(channel.answer.called)
            changes[1]["diff"] = "+b"
            for _ in range(2):
                self.assertEqual((await read(action=action))["status"], "resolved")
            channel.answer.assert_awaited_once_with(owner.thread, 6, {"decision": "accept"})
            channel.pending.pop(6)
            self.assertEqual((await read(action=action))["status"], "closed")
