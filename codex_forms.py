"""Answer a delivered Codex async question as an explicit user reply to its owning turn."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import uuid

from websockets import unix_connect
from websockets.exceptions import WebSocketException

from agent_prompts import codex_queued_questions
from hermes_forms import Receipts, decode_answers


def thread_for_title(home, title):
    # Codex adds the spinner or the Action Required segment to its window title.
    parts = {re.sub(r"^[⠀-⣿]\\s*", "", part.strip()) for part in title.split(" | ")}
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


async def exchange(home, pane, pid, snapshot, submission, rpc):
    screen, title, cwd = snapshot()
    if not codex_queued_questions(screen):
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


async def on_host(home, pane, pid, snapshot, submission=None):
    sock = home/".codex/app-server-control/app-server-control.sock"
    try:
        async with asyncio.timeout(8), unix_connect(str(sock), "ws://localhost", open_timeout=2,
                                                    close_timeout=.3, max_size=2_000_000) as ws:
            seq = 0
            async def rpc(method, params):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"id":seq, "method":method, "params":params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq:
                        if "error" in msg:
                            raise ValueError("Codex declined the request")
                        return msg["result"]
            await rpc("initialize", {"clientInfo":{"name":"tmuxweb","version":"1"},
                                     "capabilities":{"experimentalApi":True}})
            await ws.send(json.dumps({"method":"initialized"}))
            return await exchange(home, pane, pid, snapshot, submission, rpc)
    except (WebSocketException, OSError, ValueError, TimeoutError, KeyError):
        return {"status":"unconfirmed" if submission else "unavailable"}
