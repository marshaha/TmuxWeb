"""A Claude Code stream-json session with pane-bound tool permission callbacks."""
import asyncio
from collections import OrderedDict
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import sys
import uuid

import chat_ipc as ipc
from hermes_forms import decode_answers, short
from plugins.agent.hooks.observe import record


def permission_fields(request):
    tool, args = request["tool_name"], request["input"]
    if not short(tool, 200) or not isinstance(args, dict) or not short(request.get("tool_use_id"), 256):
        raise ValueError("invalid permission")
    if len(ipc.encode(request)) > ipc.MAX_FORM:
        raise ValueError("request too large to display in full")
    origin = request.get("agent_id")
    origin = "子 agent · " + origin if short(origin, 256) else ""
    if tool != "AskUserQuestion":
        if tool == "ExitPlanMode" and not short(args.get("plan"), ipc.MAX_FORM):
            raise ValueError("plan contents unavailable")
        detail = json.dumps(args, ensure_ascii=False, indent=2)
        return [{"id": "decision", "title": tool, "description": "\n".join(filter(None, (origin, detail))),
                 "choices": ["仅允许这一次", "拒绝"], "multi": False, "other": False}]
    questions = args.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 5:
        raise ValueError("invalid questions")
    fields = []
    for i, question in enumerate(questions):
        title, options = question["question"], question["options"]
        multi = question.get("multiSelect", False)
        if not short(title, 2000) or not isinstance(options, list) or not 1 <= len(options) <= 24 or type(multi) is not bool:
            raise ValueError("invalid question")
        labels = [option["label"] for option in options]
        if not all(short(label, 800) for label in labels) or len(set(labels)) != len(labels):
            raise ValueError("invalid choices")
        descriptions = [o["label"] + " · " + o["description"] for o in options if o.get("description")]
        fields.append({"id": str(i), "title": title, "choices": labels, "multi": multi, "other": True,
                       "description": "\n".join(filter(None, (origin, *descriptions)))})
    if len(set(field["title"] for field in fields)) != len(fields):
        raise ValueError("ambiguous answer keys")
    return fields


def control_reply(rid, result):
    return {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": result}}


class Owner:
    def __init__(self, changed=lambda: None):
        self.changed = changed
        self.session = str(uuid.uuid4())
        self.requests = OrderedDict()
        self.completed = OrderedDict()
        self.busy = False

    def receive(self, msg, write):
        kind, rid = msg.get("type"), msg.get("request_id")
        if msg.get("session_id") not in (None, self.session):
            return
        if kind == "control_cancel_request":
            if self.requests.pop(rid, None):
                self.changed()
        elif kind == "control_request":
            request = msg["request"]
            if not short(rid, 256) or rid in self.requests:
                raise ValueError("duplicate or invalid control request")
            if request.get("subtype") != "can_use_tool":
                write({"type": "control_response", "response": {"subtype": "error", "request_id": rid,
                                                               "error": "Unsupported control request"}})
                return
            try:
                fields = permission_fields(request)
                if len(self.requests) >= 32:
                    raise ValueError("too many pending requests")
            except (ValueError, TypeError, KeyError):
                write(control_reply(rid, {"behavior": "deny", "message": "请求超出 Chat 可完整展示的范围"}))
                return
            form = {"provider": "Claude Code", "kind": "question" if request["tool_name"] == "AskUserQuestion" else "approval",
                    "request_id": rid, "revision": secrets.token_hex(32), "questions": fields}
            self.requests[rid] = form, request
            self.changed()
            print("Claude Code 等待你在 Chat 中确认。", flush=True)
        elif kind == "result":
            self.busy = False
            # Background subagents can still hold callbacks after the main
            # turn emits its result. Their cancellation names the request.
            for rid, (_, request) in list(self.requests.items()):
                if not request.get("agent_id"):
                    write(control_reply(rid, {"behavior": "deny", "message": "Turn ended"}))
                    del self.requests[rid]
                    self.changed()
            if msg.get("is_error"):
                print(str(msg.get("result") or "Claude Code 请求失败"), flush=True)
        elif kind == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    print("".join(c for c in block["text"] if c.isprintable() or c in "\n\t"), flush=True)

    def handle(self, action, write):
        if action is None:
            forms = [form for form, _ in self.requests.values()]
            return {"status": "open", "form": forms[0], "forms": forms} if forms else {"status": "closed"}
        if action == {"binding": True}:
            return {"session": self.session}
        if "prompt" in action:
            text = ipc.string(action["prompt"], 100000)
            if self.busy:
                return {"status": "busy"}
            write({"type": "user", "session_id": self.session, "parent_tool_use_id": None,
                   "message": {"role": "user", "content": text}})
            self.busy = True
            return {"status": "submitted"}
        revision = action.get("revision")
        if revision in self.completed:
            return {"status": "resolved" if self.completed[revision] == action.get("answers") else "closed"}
        for rid, (form, request) in self.requests.items():
            if form["revision"] != revision:
                continue
            try:
                answers = decode_answers(form["questions"], action.get("answers"))
            except ValueError:
                return {"status": "invalid"}
            args = request["input"]
            if form["kind"] == "question":
                selected = {q["title"]: ", ".join(json.loads(answers[q["id"]])) if q["multi"] else answers[q["id"]]
                            for q in form["questions"]}
                args = dict(args, answers=selected)
            result = {"behavior": "allow", "updatedInput": args, "toolUseID": request["tool_use_id"]}
            if form["kind"] == "approval" and answers["decision"] == "拒绝":
                result = {"behavior": "deny", "message": "User declined", "toolUseID": request["tool_use_id"]}
            write(control_reply(rid, result))
            self.requests.pop(rid)
            self.changed()
            self.completed[revision] = action["answers"]
            if len(self.completed) > 64:
                self.completed.popitem(last=False)
            return {"status": "resolved"}
        return {"status": "closed"}


async def run(binary):
    pane, pid, path, listener = ipc.listener_paths("claude")
    changed = asyncio.Event()
    owner = Owner(changed.set)
    notice_db = Path.home()/".config/tmuxweb-agent/interactions.sqlite3"
    def publish(rows):
        record(notice_db, pane, pid, "claude", (owner.session, "", "managed-snapshot", "", rows), time.time())
    stopping = False
    async def notices():
        while not stopping:
            if owner.requests:
                try:
                    await asyncio.wait_for(changed.wait(), 600)
                except asyncio.TimeoutError:
                    pass
            else:
                await changed.wait()
            changed.clear()
            if stopping:
                break
            rows = [("chat:" + rid, "需要回答问题" if form["kind"] == "question" else "等待工具审批")
                    for rid, (form, _) in owner.requests.items()]
            try:
                await asyncio.to_thread(publish, rows)
            except (sqlite3.Error, OSError):
                print("收件箱通知写入失败，Chat 仍可读取当前请求。", file=sys.stderr, flush=True)
                await asyncio.sleep(2)
                changed.set()

    child = await asyncio.create_subprocess_exec(binary, "--print", "--verbose", "--input-format", "stream-json",
        "--output-format", "stream-json", "--permission-prompt-tool", "stdio", "--session-id", owner.session,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=ipc.MAX_FRAME)

    def write(frame):
        child.stdin.write(ipc.encode(frame))

    async def serve(reader, writer):
        try:
            ipc.peer(writer.get_extra_info("socket"))
            msg = json.loads(await asyncio.wait_for(reader.readline(), 2))
            if msg.get("pane") == pane and ipc.foreground_owner(pid, os.getpid()):
                result = owner.handle(msg.get("action"), write)
                await child.stdin.drain()
                writer.write(ipc.encode(result))
                await writer.drain()
        except (ValueError, TypeError, OSError, KeyError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()

    async def messages():
        while line := await child.stdout.readline():
            owner.receive(json.loads(line), write)
            await child.stdin.drain()

    notifier = asyncio.create_task(notices())
    try:
        async with await asyncio.start_unix_server(serve, path=listener, limit=ipc.MAX_FRAME):
            path.symlink_to(listener.name)
            print("Claude Code 结构化会话已启动。请在 Chat 中发送消息和作答，Ctrl+C 退出。", flush=True)
            await messages()
        if await child.wait():
            raise RuntimeError("Claude Code 进程退出，请核对已安装的 CLI 和登录状态")
    finally:
        stopping = True
        changed.set()
        await notifier
        try:
            await asyncio.to_thread(publish, [])
        except (sqlite3.Error, OSError):
            print("收件箱通知清除失败", file=sys.stderr, flush=True)
        if path.is_symlink() and os.readlink(path) == listener.name:
            path.unlink(missing_ok=True)
        listener.unlink(missing_ok=True)
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 3)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
