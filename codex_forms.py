"""Answer a delivered Codex async question as an explicit user reply to its owning turn."""
import asyncio
from contextlib import closing
import sqlite3
import time
import hashlib
import json
from pathlib import Path
import re
import shlex
import uuid

from websockets import unix_connect
from websockets.exceptions import WebSocketException

from agent_prompts import codex_command_approval, codex_queued_questions, codex_tool_menu
from hermes_forms import Receipts, decode_answers


def thread_for_title(home, title):
    # Codex adds the spinner or the Action Required segment to its window title.
    parts = {re.sub(r"^[⠀-⣿]\s*", "", part.strip()) for part in title.split(" | ")}
    names = {}
    with (home / ".codex/session_index.jsonl").open() as index:
        for line in index:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                names[row["id"]] = row.get("thread_name")
    matches = [(tid, name) for tid, name in names.items() if name and name in parts]
    return matches[0] if len(matches) == 1 else (None, None)


def question_from_rollout(path, turn):
    # Read only the recent tail, including the turn ID on the delivery event.
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 2_000_000))
        if stream.tell():
            stream.readline()
        latest = None
        for line in stream:
            if b'"event_msg"' not in line and b'"user"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or not isinstance(payload := event.get("payload"), dict):
                continue
            if event.get("type") == "response_item" and payload.get("role") == "user":
                latest = None
            if event.get("type") != "event_msg":
                continue
            item = payload.get("item") or {}
            if payload.get("type") in ("task_started", "turn_aborted", "task_complete"):
                latest = None
            elif payload.get("type") == "item_completed" and isinstance(item, dict):
                if item.get("type") == "UserMessage":
                    latest = None
                elif item.get("type") == "AgentMessage" and item.get("delivery") == "async":
                    latest = item if payload.get("turn_id") == turn else None
    return latest


def fields_for(item):
    questions = (item or {}).get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 8 or not item.get("id"):
        return []
    fields = []
    for n, question in enumerate(questions):
        if not isinstance(question, dict):
            return []
        title = question.get("title")
        choices = question.get("options") or []
        if (not isinstance(title, str) or not 0 < len(title) <= 640 or not isinstance(choices, list)
                or len(choices) > 12 or any(not isinstance(c, str) or not 0 < len(c) <= 400 for c in choices)):
            return []
        fields.append({"id":str(n), "title":title, "choices":choices, "multi":False, "other":True})
    return fields


def hook_rows(home, pane, pid):
    path = home / ".config/tmuxweb-agent/interactions.sqlite3"
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.3)) as db:
            return db.execute("SELECT session,request,detail FROM waits WHERE pane=? AND pid=? "
                              "AND provider='codex' AND title='等待工具审批' AND at>? LIMIT 65",
                              (pane, pid, time.time() - 3600)).fetchall()
    except sqlite3.Error:
        return []


def rollout_patches(home, path, thread, turn, item_ids, cwd):
    """Only recover tool arguments for an already replayed live file request."""
    if not path or not item_ids:
        return {}
    try:
        path = Path(path).resolve()
        if not path.is_relative_to((home/".codex/sessions").resolve()) or not path.name.endswith(thread + ".jsonl"):
            return {}
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 2_000_000))
            if stream.tell():
                stream.readline()
            active, matched = False, {}
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                p = row.get("payload")
                if not isinstance(p, dict):
                    continue
                kind = row.get("type")
                if kind == "turn_context" or (kind == "event_msg" and p.get("type") == "task_started"):
                    active = p.get("turn_id") == turn
                    if not active:
                        matched.clear()
                elif kind == "event_msg" and p.get("type") in ("turn_aborted", "task_complete"):
                    active = False
                    matched.clear()
                elif active and kind == "response_item" and p.get("call_id") in item_ids:
                    rid = p["call_id"]
                    if rid in matched:
                        matched[rid] = None
                        continue
                    patch, cmd = "", ""
                    if p.get("name") == "apply_patch" and p.get("type") == "custom_tool_call":
                        patch = p.get("input")
                    elif p.get("name") == "exec_command" and p.get("type") == "function_call":
                        args = json.loads(p["arguments"])
                        if args.get("workdir") not in (None, cwd):
                            continue
                        cmd = args.get("cmd", "")
                        # An exact quoted heredoc has no shell expansion. Never
                        # infer a patch from a command pipeline or extra commands.
                        here = re.fullmatch(r"""apply_patch[ \t]+<<[ \t]*(['"])([A-Za-z_][A-Za-z_0-9]*)\1[ \t]*\n(.*?)\n\2\n?""", cmd, re.S)
                        if here and here[2] not in here[3].splitlines():
                            patch = here[3]
                    matched[rid] = (patch, cmd) if isinstance(patch, str) and 0 < len(patch) <= 48000 else None
            return {key: value for key, value in matched.items() if value}
    except (ValueError, KeyError, TypeError, OSError):
        return {}


def describe_patch(patch):
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return None
    paths = []
    for line in lines[1:-1]:
        if line.startswith("*** "):
            if line == "*** End of File":
                continue
            marker = re.fullmatch(r"\*\*\* (Add File|Update File|Delete File|Move to): (.+)", line)
            if not marker:
                return None
            paths.append(marker[1] + " · " + marker[2])
    return "\n".join(paths) + "\n\n" + patch if paths else None


def hook_matches(row, request, raw=None):
    thread, key, detail = row
    p = request["params"]
    if thread != p.get("threadId") or not key or key == "permission":
        return False
    if request["method"] == "item/fileChange/requestApproval":
        if key == p.get("itemId") and detail in ("apply_patch", "ApplyPatch"):
            return True
        command = raw[1] if raw else None
    elif request["method"] == "item/commandExecution/requestApproval":
        command = p.get("command")
    else:
        return False
    if not isinstance(command, str) or not command or not detail.startswith("command-sha256:"):
        return False
    digest = detail.split(":", 1)[1]
    group = hashlib.sha256((p["turnId"] + ":" + digest).encode()).hexdigest()
    commands = [command]
    try:
        argv = shlex.split(command)
        if len(argv) == 3 and argv[0] in ("/bin/sh", "/bin/bash", "/bin/zsh", "/usr/bin/bash", "/usr/bin/zsh",
                                          "/usr/local/bin/zsh") and argv[1] == "-lc" and command_matches_menu(
                command, {"options": [{"description": "$ " + argv[2]}]}):
            commands.append(argv[2])
    except ValueError:
        pass
    return ((key == p.get("itemId") or key.startswith("codex:" + group + ":"))
            and any(hashlib.sha256(c.encode()).hexdigest() == digest for c in commands))


def bound_details(request, items, raw=None):
    p = request["params"]
    if request["method"] == "item/commandExecution/requestApproval":
        choices = p.get("availableDecisions")
        if choices is None:
            choices = ["accept", "decline"]
        if (not isinstance(choices, list) or "accept" not in choices or p.get("kind", "command") != "command"
                or p.get("networkApprovalContext") or p.get("additionalPermissions")
                or not ("decline" in choices or "cancel" in choices)):
            return None
        details = [p["command"]] + [str(p[k]) for k in ("cwd", "environmentId", "reason") if p.get(k)]
        return ("允许执行这条命令？", "\n".join(details), "decline" if "decline" in choices else "cancel")
    if p.get("grantRoot"):
        return None
    matches = [item for item in items if item.get("id") == p["itemId"] and item.get("type") == "fileChange"]
    if not matches or len(matches) == 1 and not matches[0].get("changes"):
        detail = describe_patch(raw[0]) if raw else None
        return ("允许这些文件变更？", detail, "decline") if detail else None
    if len(matches) != 1:
        return None
    paths, patches = [], []
    for change in matches[0]["changes"]:
        path, kind, diff = change.get("path"), change.get("kind") or {}, change.get("diff")
        if not isinstance(path, str) or not path or kind.get("type") not in ("add", "update", "delete") or not isinstance(diff, str):
            return None
        label = kind["type"] + " " + path
        if kind.get("move_path"):
            label += " → " + kind["move_path"]
        paths.append(label)
        patches.append(label + "\n" + diff)
    return "允许这些文件变更？", "\n".join(paths) + "\n\n" + "\n\n".join(patches), "decline"


async def bound_approvals(home, pane, pid, snapshot, submission, rpc, channel, rows, cwd):
    threads = {row[0] for row in rows}
    if len(threads) != 1 or len(rows) > 64 or not cwd:
        return {"status": "closed"}
    thread = threads.pop()
    state = (await rpc("thread/read", {"threadId": thread, "includeTurns": False}))["thread"]
    if state.get("id") != thread or state.get("cwd") != cwd or state.get("status", {}).get("type") != "active":
        return {"status": "closed"}
    resumed = (await rpc("thread/resume", {"threadId": thread, "excludeTurns": True}))["thread"]
    if resumed.get("id") != thread or resumed.get("cwd") != cwd:
        return {"status": "closed"}
    await channel.wait_replay()
    files = any(r["method"] == "item/fileChange/requestApproval" for r in channel.pending.values())
    turns = (await rpc("thread/turns/list", {"threadId": thread, "limit": 1,
                                             "itemsView": "full" if files else "notLoaded"}))["data"]
    if not turns or turns[0].get("status") != "inProgress":
        return {"status": "closed"}
    turn = turns[0]["id"]
    pending = [r for r in channel.pending.values() if r.get("params", {}).get("turnId") == turn
               and r["params"].get("threadId") == thread and r["params"].get("itemId")]
    file_ids = {r["params"]["itemId"] for r in pending if r["method"] == "item/fileChange/requestApproval"}
    raw = rollout_patches(home, state.get("path"), thread, turn, file_ids, cwd)
    def matches_request(row, r):
        return hook_matches(row, r, raw.get(r["params"]["itemId"]))
    forms = []
    live_rows = hook_rows(home, pane, pid)
    if snapshot()[2] != cwd:
        return {"status": "closed"}
    for request in pending:
        matches = [row for row in rows if row in live_rows and matches_request(row, request)]
        if len(matches) != 1 or sum(matches_request(matches[0], r) for r in pending) != 1:
            continue
        items = turns[0].get("items", [])
        patches = getattr(channel, "patches", {})
        key = (thread, turn, request["params"]["itemId"])
        if isinstance(patches, dict) and key in patches:
            items = [{"id": key[2], "type": "fileChange", "changes": patches[key]}]
        details = bound_details(request, items, raw.get(key[2]))
        if not details or len(details[1].encode()) > 48000:
            continue
        title, detail, deny = details
        if request["method"] == "item/fileChange/requestApproval":
            reason = request["params"].get("reason")
            detail = "会话目录：" + cwd + "\n" + (str(reason) + "\n" if reason else "") + detail
            if len(detail.encode()) > 48000:
                continue
        choices = {"仅批准这一次": {"decision": "accept"}, "拒绝": {"decision": deny}}
        args = (thread, turn, request, channel, title, detail, choices)
        form = (await approval_form(home, pane, pid, None, *args))["form"]
        form["request_id"] = str(request["id"])
        if submission and form["revision"] == submission.get("revision"):
            return await approval_form(home, pane, pid, submission, *args)
        forms.append(form)
    if submission or not forms:
        return {"status": "closed"}
    return {"status": "open", "form": forms[0], "forms": forms}


async def exchange(home, pane, pid, snapshot, submission, rpc, channel=None):
    screen, title, cwd = snapshot()
    rows = hook_rows(home, pane, pid) if channel else []
    if rows:
        bound = await bound_approvals(home, pane, pid, snapshot, submission, rpc, channel, rows, cwd)
        if bound["status"] != "closed" or not (codex_tool_menu(screen) or codex_queued_questions(screen)):
            return bound
    tool_menu = codex_tool_menu(screen)
    command_menu = codex_command_approval(screen)
    if not (command_menu or tool_menu or codex_queued_questions(screen)):
        return {"status": "closed"}
    thread, name = thread_for_title(home, title)
    if not thread:
        return {"status": "closed"}
    status = (await rpc("thread/read", {"threadId":thread, "includeTurns":False}))["thread"]
    if (status.get("id") != thread or status.get("name") != name or status.get("cwd") != cwd
            or (status.get("status") or {}).get("type") != "active" or not status.get("path")):
        return {"status": "closed"}
    path = Path(status["path"]).resolve()
    if not path.is_relative_to((home / ".codex/sessions").resolve()) or not path.stem.endswith(thread):
        return {"status": "closed"}
    turns = (await rpc("thread/turns/list", {"threadId":thread, "limit":1, "itemsView":"notLoaded"}))["data"]
    if not turns or turns[0].get("status") != "inProgress":
        return {"status": "closed"}
    turn = turns[0]["id"]
    if tool_menu or command_menu:
        if channel is None:
            return {"status": "unavailable"}
        resumed = (await rpc("thread/resume", {"threadId": thread, "excludeTurns": True}))["thread"]
        if resumed.get("id") != thread or resumed.get("cwd") != cwd or resumed.get("name") != name:
            return {"status": "closed"}
        # Replay is sent on this connection after resume. A subsequent RPC
        # gives in-flight replay/resolution messages a chance to arrive.
        await channel.wait_replay()
        live = (await rpc("thread/turns/list", {"threadId":thread, "limit":1, "itemsView":"notLoaded"}))["data"]
        if not live or live[0].get("id") != turn or live[0].get("status") != "inProgress":
            return {"status": "closed"}
        pending = list(channel.pending.values())
        if len(pending) != 1:
            return {"status": "closed"}
        if command_menu:
            return await command_approval(home, pane, pid, snapshot, submission, thread, name, cwd,
                                          turn, command_menu, pending[0], channel)
        return await tool_approval(home, pane, pid, snapshot, submission, thread, name, cwd,
                                   turn, tool_menu, pending[0], channel)
    item = question_from_rollout(path, turn)
    fields = fields_for(item)
    # The TUI can scroll the question text away. Its own status line continues
    # to identify the thread, independently of the tmux window title.
    if not any(name in [part.strip() for part in row.split(" · ")] for row in screen.splitlines()[-8:]):
        rows = [row.strip() for row in screen.splitlines() if row.strip()]
        for field in fields:
            block = ["• " + value for value in [field["title"], *field["choices"]]]
            if not any(rows[i:i+len(block)] == block for i in range(len(rows)) if rows[i] == block[0]):
                return {"status":"closed"}
    if not fields or len(fields) != codex_queued_questions(screen):
        return {"status": "closed"}
    identity = [pane, pid, thread, turn, item["id"], fields]
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    form = {"provider":"Codex", "kind":"question", "revision":revision, "questions":fields}
    if submission is None:
        return {"status":"open", "form":form}
    if submission.get("revision") != revision:
        return {"status":"closed"}
    try:
        answers = decode_answers(fields, submission.get("answers"))
    except ValueError:
        return {"status":"invalid"}
    receipts = Receipts(home/".config/tmuxweb-agent", pane, pid, submission)
    previous = receipts.access("claim", retry_pending=False)
    if previous:
        return previous
    reply = "\n".join(q["title"] + "\n" + answers[q["id"]] for q in fields)
    current, now_title, now_cwd = snapshot()
    if (thread_for_title(home, now_title)[0] != thread or cwd != now_cwd or not codex_queued_questions(current)
            or question_from_rollout(path, turn) != item):
        result = "closed"
    else:
        # The async tool delivers an agent message. Its answer is a user message,
        # unlike the blocking item/tool/requestUserInput server request.
        ack = await rpc("turn/steer", {"threadId":thread, "expectedTurnId":turn,
                         "clientUserMessageId":str(uuid.UUID(revision[:32])),
                         "input":[{"type":"text","text":reply,"text_elements":[]}]})
        result = "resolved" if ack.get("turnId") == turn else "unconfirmed"
    receipts.access("finish", result)
    return {"status":result}


def command_matches_menu(command, menu):
    display = menu["options"][0]["description"][2:]
    if command == display:
        return True
    # The app-server serializes argv with shell quoting. The TUI displays the
    # script argument of its login shell. Only unwrap a literal argument;
    # substitutions and shell operators outside that argument must stay visible.
    match = re.fullmatch(r"(/(?:usr/(?:local/)?)?bin/(?:zsh|bash|sh)) -lc (.+)", command, re.S)
    if not match:
        return False
    quoted = "'" + display.replace("'", "'\\''") + "'"
    return match[2] in (shlex.quote(display), quoted)


async def command_approval(home, pane, pid, snapshot, submission, thread, name, cwd, turn, menu, request, channel):
    params = request.get("params") or {}
    if (request.get("method") != "item/commandExecution/requestApproval" or type(request.get("id")) not in (int, str)
            or params.get("threadId") != thread or params.get("turnId") != turn or not params.get("itemId")
            or params.get("kind", "command") != "command" or params.get("networkApprovalContext")
            or params.get("additionalPermissions")):
        return {"status": "closed"}
    command = params.get("command")
    if not isinstance(command, str) or not command or len(command) > 10000:
        return {"status": "closed"}
    # The native request is authoritative. Require the same command on the
    # current screen. Do not normalize shell whitespace or guess truncated text.
    if not command_matches_menu(command, menu):
        return {"status": "closed"}
    screen, now_title, now_cwd = snapshot()
    live = codex_command_approval(screen)
    if (now_cwd != cwd or thread_for_title(home, now_title) != (thread, name)
            or not live or live["revision"] != menu["revision"]):
        return {"status": "closed"}
    decisions = params.get("availableDecisions")
    if decisions is None:
        decisions = ["accept", "decline"]
    if not isinstance(decisions, list) or "accept" not in decisions or not ("decline" in decisions or "cancel" in decisions):
        return {"status": "closed"}
    choices = {"仅批准这一次": {"decision": "accept"},
               "拒绝此次命令": {"decision": "decline" if "decline" in decisions else "cancel"}}
    details = [command]
    for field, label in (("cwd", "目录"), ("environmentId", "环境"), ("reason", "原因")):
        if params.get(field):
            details.append(label + "：" + str(params[field]))
    if len("\n".join(details)) > 12000:
        return {"status": "unavailable"}
    return await approval_form(home, pane, pid, submission, thread, turn, request, channel,
                               "允许执行这条命令？", "\n".join(details), choices)


async def tool_approval(home, pane, pid, snapshot, submission, thread, name, cwd, turn, menu, request, channel):
    params = request.get("params") or {}
    meta = params.get("_meta")
    schema = params.get("requestedSchema")
    rid = request.get("id")
    if (request.get("method") != "mcpServer/elicitation/request" or type(rid) not in (int, str)
            or params.get("threadId") != thread or params.get("turnId") not in (None, turn)
            or params.get("mode") != "form" or not isinstance(schema, dict)
            or schema.get("type") != "object" or schema.get("properties") != {} or schema.get("required")
            or not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call"):
        return {"status": "closed"}
    scopes = meta.get("persist", [])
    if isinstance(scopes, str):
        scopes = [scopes]
    if not isinstance(scopes, list) or any(scope not in ("session", "always") for scope in scopes):
        return {"status": "closed"}
    choices = {"Allow": ("仅允许这一次", "accept", None)}
    if "session" in scopes:
        choices["Allow for this session"] = ("在此会话中允许", "accept", {"persist": "session"})
    if "always" in scopes:
        choices["Always allow"] = ("始终允许此工具", "accept", {"persist": "always"})
    choices["Cancel"] = ("取消此次调用", "cancel", {"codex_approval_cancellation_reason": "user"})
    title, server = params.get("message"), params.get("serverName")
    if tuple(choices) != menu or not isinstance(title, str) or not title.strip() or not isinstance(server, str) or not server:
        return {"status": "closed"}
    detail = server
    for key in ("tool_name", "tool_title", "tool_params", "tool_params_display"):
        if key in meta and meta[key] is not None:
            if key == "tool_params_display" and meta.get("tool_params") is not None:
                continue
            detail += "\n" + json.dumps(meta[key], ensure_ascii=False)
    if len(title) + len(detail) > 12000:
        return {"status": "unavailable"}
    screen, now_title, now_cwd = snapshot()
    if now_cwd != cwd or thread_for_title(home, now_title) != (thread, name) or codex_tool_menu(screen) != menu:
        return {"status": "closed"}
    if " ".join(title.split()) not in " ".join(screen.split()):
        return {"status": "closed"}
    replies = {label: {"action": action, "content": None, "_meta": metadata}
               for label, action, metadata in choices.values()}
    return await approval_form(home, pane, pid, submission, thread, turn, request, channel, title, detail, replies)


async def approval_form(home, pane, pid, submission, thread, turn, request, channel, title, detail, replies):
    fields = [{"id": "decision", "title": title, "description": detail, "multi": False, "other": False,
               "choices": list(replies)}]
    identity = [pane, pid, thread, turn, request, fields]
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if submission is None:
        return {"status": "open", "form": {"provider": "Codex", "kind": "approval",
                                           "approval_type": "command" if request["method"] == "item/commandExecution/requestApproval" else (
                                               "file" if request["method"] == "item/fileChange/requestApproval" else "tool"),
                                           "revision": revision, "questions": fields}}
    if submission.get("revision") != revision:
        return {"status": "closed"}
    try:
        choice = decode_answers(fields, submission.get("answers"))["decision"]
    except ValueError:
        return {"status": "invalid"}
    receipts = Receipts(home/".config/tmuxweb-agent", pane, pid, submission)
    prior = receipts.access("claim", retry_pending=False)
    if prior:
        return prior
    result = await channel.answer(thread, request["id"], replies[choice])
    receipts.access("finish", result)
    return {"status": result}


class RequestChannel:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.pending = {}
        self.resolved = set()
        self.patches = {}

    async def receive(self):
        msg = json.loads(await self.ws.recv())
        if "method" in msg and "id" in msg:
            self.pending[msg["id"]] = msg
        elif msg.get("method") == "item/fileChange/patchUpdated":
            event = msg["params"]
            self.patches[event["threadId"], event["turnId"], event["itemId"]] = event["changes"]
        elif msg.get("method") == "serverRequest/resolved":
            event = msg["params"]
            current = self.pending.get(event["requestId"])
            if current and current.get("params", {}).get("threadId") == event["threadId"]:
                self.pending.pop(event["requestId"])
            self.resolved.add((event["threadId"], event["requestId"]))
        return msg

    async def rpc(self, method, params):
        self.seq += 1
        # Server requests use their own ID space; they can arrive before a reply.
        rid = "tw-" + str(self.seq)
        await self.ws.send(json.dumps({"id": rid, "method": method, "params": params}))
        while True:
            msg = await self.receive()
            if "method" not in msg and msg.get("id") == rid:
                if "error" in msg:
                    raise ValueError("Codex declined the request")
                return msg["result"]

    async def wait_replay(self):
        try:
            async with asyncio.timeout(1):
                while not self.pending and not self.resolved:
                    await self.receive()
        except TimeoutError:
            pass

    async def answer(self, thread, rid, result):
        await self.ws.send(json.dumps({"id": rid, "result": result}))
        while (thread, rid) not in self.resolved:
            await self.receive()
        return "resolved"


async def on_host(home, pane, pid, snapshot, submission=None):
    sock = home/".codex/app-server-control/app-server-control.sock"
    try:
        first = snapshot()
        rows = hook_rows(home, pane, pid)
        if not rows and not (codex_tool_menu(first[0]) or codex_command_approval(first[0]) or codex_queued_questions(first[0])):
            return {"status": "closed"}

        async with asyncio.timeout(8), unix_connect(str(sock), "ws://localhost", open_timeout=2,
                                                    close_timeout=.3, compression=None, max_size=2_000_000) as ws:
            channel = RequestChannel(ws)
            await channel.rpc("initialize", {"clientInfo":{"name":"tmuxweb","version":"1"},
                                            "capabilities":{"experimentalApi":True}})
            await ws.send(json.dumps({"method":"initialized"}))
            return await exchange(home, pane, pid, snapshot, submission, channel.rpc, channel)
    except (WebSocketException, OSError, ValueError, TimeoutError, KeyError):
        return {"status":"unconfirmed" if submission else "unavailable"}
