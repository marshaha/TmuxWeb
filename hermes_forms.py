"""Read and answer a pane-bound Hermes gateway request on the host that owns it.

This file also runs as a standalone helper in Hermes' own Python environment.
The current foreground Hermes process and its terminal breadcrumb establish session
identity. A native hook can corroborate it. Capture and history never supply answers.
"""
from collections import OrderedDict
import threading
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import time
from contextlib import closing


def terminal_session(pid, home):
    """Match Hermes' per-tty breadcrumb to the current foreground CLI lifetime on Linux."""
    try:
        tty = os.readlink(f"/proc/{pid}/fd/0")
        if not re.fullmatch(r"/dev/pts/[0-9]+", tty):
            return None
        def stat(process):
            return Path(f"/proc/{process}/stat").read_text().rsplit(")", 1)[1].split()
        parent = stat(pid)
        foreground = parent[5]  # tpgid, field 8
        if not foreground.isdigit():
            return None
        process = stat(foreground)
        if process[3:5] != parent[3:5] or process[5] != foreground:
            return None
        argv = Path(f"/proc/{foreground}/cmdline").read_bytes().split(b"\0")
        root = home/".hermes/hermes-agent"
        launchers = {os.fsencode(home/".local/bin/hermes"), os.fsencode(root/".hermes/bin/hermes"),
                     os.fsencode(root/"venv/bin/hermes")}
        python = Path(os.readlink(f"/proc/{foreground}/exe")).name.startswith("python")
        if not (python and (any(arg in launchers for arg in argv[:2]) or (
                b"-I" in argv[:4] and any(b"from hermes_cli.main import main" in arg and
                os.fsencode(root) in arg for arg in argv[:5])))):
            return None
        key = "tty-" + tty.strip("/").replace("/", "-")
        data = json.loads((home/".hermes/terminal-sessions"/key).read_text())
        boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
        started = boot + int(process[19]) / os.sysconf("SC_CLK_TCK")
        if not started <= data["ts"] <= time.time()+1 or not short(data.get("session_id"), 160):
            return None
        return (data["session_id"], f"tty:{foreground}:{process[19]}", "live", "")
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration):
        return None


def binding(pane, pid, home):
    path = home / ".config/tmuxweb-agent/interactions.sqlite3"
    native = terminal_session(pid, home)
    if not native or not path.is_file():
        return native
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.5)) as db:
        rows = db.execute("SELECT session,request,title,detail FROM waits WHERE pane=? AND pid=? "
                          "AND provider='hermes' AND title!='' AND at>? LIMIT 2",
                          (pane, pid, time.time() - 3600)).fetchall()
    if any(row[0] and row[0] != native[0] for row in rows):
        return None
    # Only the foreground CLI and the gateway own identity. Sibling hook
    # notifications may arrive or clear while another request stays open.
    return native


def short(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def project(snapshot, notice, pane, pid):
    session, event, title, detail = notice
    requests = snapshot.get("open_requests", [])
    prompts = snapshot.get("prompts", [])
    if len(requests) + len(prompts) != 1:
        return None
    generation = snapshot.get("execution_generation")
    wire_approval = bool(requests and requests[0].get("method") == "approval")
    if requests and (not isinstance(requests[0].get("params"), dict)
                     or requests[0]["params"].get("session_id", session) != session):
        return None
    if requests and not wire_approval and title in ("需要回答问题", "live"):
        item = requests[0]
        if item.get("method") != "clarify":
            return None
        params = item["params"]
        questions = params.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 5:
            return None
        fields = []
        for q in questions:
            if not isinstance(q, dict) or not short(q.get("question"), 1200) or not short(q.get("qid"), 128):
                return None
            choices = q.get("choices")
            choices = [] if choices is None else choices
            if not isinstance(choices, list) or len(choices) > 24 or not all(short(c, 800) for c in choices):
                return None
            if len(set(choices)) != len(choices) or type(q.get("multi_select", False)) is not bool:
                return None
            fields.append({"id": q["qid"], "title": q["question"], "choices": choices,
                           "multi": q.get("multi_select", False), "other": True})
        if len({f["id"] for f in fields}) != len(fields) or (title != "live" and "\n".join(f["title"] for f in fields) != detail):
            return None
        # A terminal viewer has already locked answers. Let that viewer finish this batch.
        if params.get("answers"):
            return None
        rid, kind = item.get("id"), "clarify"
    elif (prompts or wire_approval) and title in ("等待操作审批", "live"):
        item = requests[0]["params"] if wire_approval else prompts[0]
        if wire_approval:
            if not short(item.get("request_id"), 160):
                return None
        elif (item.get("kind") != "approval" or type(generation) is not int
              or item.get("execution_generation") != generation):
            return None
        if ("edit" in item or
                not short(item.get("command"), 4096) or
                not {"once", "deny"}.issubset(item.get("choices") or [])):
            return None
        rid, kind = (requests[0]["id"] if wire_approval else item.get("prompt_id")), "approval"
        fields = [{"id": "decision", "title": item["command"], "choices": ["仅允许这一次", "拒绝"],
                   "multi": False, "other": False}]
        if item.get("description"):
            if not short(item["description"], 2000):
                return None
            fields[0]["description"] = item["description"]
    else:
        return None
    if not short(rid, 160):
        return None
    identity = [pane, pid, session, event, snapshot.get("replay_epoch"), rid, generation, kind, fields,
                item.get("request_id") if wire_approval else None]
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {"revision": revision, "kind": kind, "questions": fields, "wire_approval": wire_approval}, rid


def decode_answers(fields, answers):
    if not isinstance(answers, dict) or answers.keys() != {q["id"] for q in fields}:
        raise ValueError("incomplete answers")
    result = {}
    for q in fields:
        selection = answers[q["id"]]
        if not isinstance(selection, dict) or selection.keys() - {"indices", "other"}:
            raise ValueError("invalid selection")
        indices, other = selection.get("indices"), selection.get("other")
        if (not isinstance(indices, list) or len(indices) > len(q["choices"]) or
                any(type(i) is not int or not 0 <= i < len(q["choices"]) for i in indices) or
                len(set(indices)) != len(indices)):
            raise ValueError("invalid option")
        values = [q["choices"][i] for i in sorted(indices)]
        if other is not None:
            if not q["other"] or not short(other, 2000) or not other.strip():
                raise ValueError("invalid free text")
            values.append(other)
        if not values or (not q["multi"] and len(values) != 1):
            raise ValueError("select an answer for every question")
        result[q["id"]] = json.dumps(values, ensure_ascii=False) if q["multi"] else values[0]
    return result


class Receipts:
    """Per-host receipts, shared across app workers. Store only a digest of the answer."""
    def __init__(self, folder, pane, pid, submission):
        self.folder = folder
        self.identity = (pane, pid, submission["revision"])
        self.digest = hashlib.sha256(json.dumps(submission["answers"], sort_keys=True).encode()).hexdigest()

    def access(self, operation, outcome=None, *, retry_pending=True):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.folder/"form-receipts.sqlite3", timeout=2)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts(pane,pid,revision,digest,status,at REAL,"
                       "PRIMARY KEY(pane,pid,revision))")
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if operation == "finish":
                db.execute("UPDATE receipts SET status=?,at=? WHERE pane=? AND pid=? AND revision=? AND digest=?",
                           (outcome, now, *self.identity, self.digest))
                return None
            row = db.execute("SELECT digest,status,at FROM receipts WHERE pane=? AND pid=? AND revision=?",
                             self.identity).fetchone()
            if row:
                if row[0] != self.digest:
                    return {"status": "closed" if row[1] in ("resolved","closed") else "unconfirmed"}
                if row[1] in ("resolved","closed"):
                    return {"status": row[1]}
                if not retry_pending or now - row[2] < 12:
                    return {"status": "unconfirmed"}
            if operation == "claim":
                db.execute("DELETE FROM receipts WHERE at<?", (now-7*86400,))
                db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,?)",
                           (*self.identity, self.digest, "pending", now))
        return None


def project_queue(snapshot, notice, pane, pid):
    requests = snapshot.get("open_requests", [])
    prompts = snapshot.get("prompts", [])
    result = []
    seen = set()
    for source, items, id_key in (("open_requests", requests, "id"), ("prompts", prompts, "prompt_id")):
        for item in items:
            if not isinstance(item, dict):
                continue
            rid = item.get(id_key)
            if not short(rid, 160):
                continue
            if (source, rid) in seen:
                return []
            seen.add((source, rid))
            current = project({"execution_generation": snapshot.get("execution_generation"),
                               "replay_epoch": snapshot.get("replay_epoch") or snapshot.get("epoch"), source: [item]},
                              notice, pane, pid)
            if current:
                form, _ = current
                form["request_id"] = rid
                result.append(current)
    return result


async def event_page(client, session, cursor):
    previous = cursor.get("epoch")
    def page_args(seq):
        if cursor.get("canonical"):
            return {"last_sequence": seq, **({"replay_epoch": previous} if previous and seq else {})}
        return {"last_seen": seq}
    page = await client.rpc("session.events.since", session_id=session, **page_args(cursor.get("seq", 0)))
    epoch = page.get("replay_epoch") or page.get("epoch")
    if previous and previous != epoch:
        page = await client.rpc("session.events.since", session_id=session, **page_args(0))
        epoch = page.get("replay_epoch") or page.get("epoch")
    seq = page.get("last_sequence", page.get("latest_seq"))
    if type(seq) is int and seq >= 0 and short(epoch, 160):
        cursor.update(seq=seq, epoch=epoch, canonical="last_sequence" in page)
    return page


async def exchange(client, notice, pane, pid, submission, still_current, receipts=None, cursor=None):
    session = notice[0]
    # A read of the *live* stream fails for an unknown session. Never resume a
    # historical session merely because an old hook row survived its process.
    cursor = {} if cursor is None else cursor
    live = await event_page(client, session, cursor)
    pending = live.get("open_requests", [])
    if "latest_seq" in live and "last_sequence" not in live:
        # This gateway includes every actionable request in the event result.
        # Its session.resume attaches a transport, so reads must avoid it.
        snapshot = live
    else:
        if not pending:
            if notice[2] == "需要回答问题":
                return {"status": "closed"}
            active = await client.rpc("session.active_list", current_session_id=session)
            if not any(row.get("id") == session and row.get("status") == "waiting"
                       for row in active.get("sessions", [])):
                return {"status": "closed"}
        snapshot = await client.rpc("session.resume", session_id=session, omit_messages=True)
    if snapshot.get("session_id", session) != session:
        return {"status": "closed"}
    queue = project_queue(snapshot, notice, pane, pid)
    if not queue or not still_current():
        return {"status": "closed"}
    if submission is None:
        forms = [form for form, _ in queue]
        return {"status": "open", "form": forms[0], "forms": forms}
    match = next((pair for pair in queue if pair[0]["revision"] == submission.get("revision")), None)
    if not match:
        return {"status": "closed"}
    form, rid = match
    try:
        answers = decode_answers(form["questions"], submission.get("answers"))
    except ValueError:
        return {"status": "invalid"}
    if receipts:
        existing = receipts.access("claim")
        if existing:
            return existing
    result = await respond(client, session, snapshot, form, rid, submission, answers, cursor)
    if receipts:
        receipts.access("finish", result["status"])
    return result


async def respond(client, session, snapshot, form, rid, submission, answers, cursor):
    if form["kind"] == "approval":
        choice = "once" if submission["answers"]["decision"]["indices"] == [0] else "deny"
        if not form["wire_approval"]:
            result = await client.rpc("approval.respond", session_id=session, prompt_id=rid,
                                      execution_generation=snapshot["execution_generation"], choice=choice)
            return {"status": "resolved" if result.get("status") == "resolved" else "closed"}
        payload = {"choice": choice}
    else:
        payload = {"outcome": "submitted", "answers": answers}
    # Hermes resolves the entire request under its own lock. A late response
    # from a second browser or the native TUI is discarded by the owner.
    epoch = cursor.get("epoch")
    frame = {"jsonrpc": "2.0", "id": rid, "result": payload}
    await client.websocket.send(json.dumps(frame))
    for delay in (0, .08, .2):
        if delay:
            await asyncio.sleep(delay)
        after = await event_page(client, session, cursor)
        if cursor.get("epoch") != epoch:
            return {"status": "unconfirmed"}
        for entry in after.get("events", []):
            entry = entry.get("params", entry)
            data = entry.get("payload") or {}
            if entry.get("type") == "request.cancel" and data.get("id") == rid:
                return {"status": "resolved" if data.get("reason") == "resolved" else "closed"}
    if any(entry.get("id") == rid for entry in after.get("open_requests", [])):
        return {"status": "unconfirmed"}
    return {"status": "closed"}


async def local(args):
    pane, pid = args["pane"], args["pid"]
    def read():
        return binding(pane, pid, Path.home())
    receipts = None
    if args.get("submission") is not None:
        receipts = Receipts(Path.home()/".config/tmuxweb-agent", pane, pid, args["submission"])
        existing = receipts.access("lookup")
        if existing:
            return existing
    notice = read()
    if not notice:
        return {"status": "closed"}
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint, gateway_profile_home
    from hermes_constants import get_hermes_home
    profile = Path(get_hermes_home()).resolve()
    if profile != (Path.home()/".hermes").resolve():
        return {"status": "unavailable"}
    owner = discover_gateway_endpoint(gateway_profile_home(profile), timeout=1)
    if not attachable(owner) or not owner.endpoint:
        return {"status": "unavailable"}
    async with connect_gateway(endpoint=owner.endpoint) as client:
        cursor = args.get("cursor") or {}
        if cursor.get("binding") != list(notice[:2]):
            cursor = {"binding": list(notice[:2])}
        result = await exchange(client, notice, pane, pid, args.get("submission"),
                                lambda: read() == notice, receipts, cursor)
        return dict(result, cursor=cursor)


# A watermark is only a replay optimisation. The live snapshot still
# supplies the full set of outstanding requests on every read.
_cursors = OrderedDict()
_cursor_lock = threading.Lock()


def on_host(pane, pid, submission, runner=None):
    import shlex
    import subprocess

    key = (str(getattr(runner, "host", {}).get("id", "")), pane, pid)
    with _cursor_lock:
        cursor = _cursors.get(key)
    body = json.dumps({"pane": pane, "pid": pid, "submission": submission, "cursor": cursor}).encode()
    if runner is None and submission is None and not binding(pane, pid, Path.home()):
        return {"status": "closed"}
    program = Path(__file__).read_text()
    # Hermes' packaged launcher selects the generation and ABI for this checkout.
    # Neither the session's working directory nor its PATH can select the code.
    bootstrap = """import os, sys
from pathlib import Path
root = Path.home()/'.hermes/hermes-agent'
if not (root/'hermes_cli/gateway_runtime.py').is_file():
    print('{"status":"unavailable"}')
    sys.exit(0)
sys.path.insert(0, str(root))
try:
    from hermes_cli._launchers import runtime_command
except ImportError:
    args = [str(root/'venv/bin/python'), '-I', '-c', sys.argv[1]]
else:
    args = runtime_command(root, code=sys.argv[1])
os.execv(args[0], args)
"""
    args = ["python3", "-I", "-c", bootstrap, program]
    if runner is None:
        proc = subprocess.run([sys.executable, *args[1:]], input=body, capture_output=True, timeout=9)
    else:
        proc = runner.run(shlex.join(args), input_bytes=body, timeout=9, resolve_tmux=False)
    result = json.loads(proc.stdout) if proc.returncode == 0 else {"status": "unavailable"}
    cursor = result.pop("cursor", None)
    if cursor and "seq" in cursor:
        with _cursor_lock:
            _cursors[key] = cursor
            _cursors.move_to_end(key)
            if len(_cursors) > 256:
                _cursors.popitem(last=False)
    return result


if __name__ == "__main__":
    try:
        output = asyncio.run(asyncio.wait_for(local(json.load(sys.stdin)), 5))
    except Exception:
        # Provider and transport errors may contain authenticated endpoint URLs.
        output = {"status": "unavailable"}
    print(json.dumps(output, ensure_ascii=True))
