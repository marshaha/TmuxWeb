"""Structured transcript readers for agent CLIs.

The same file runs in two places:

* imported by ``tmux_web.py`` to read panes on the local host;
* piped over SSH to ``python3 -`` on a remote host, so remote panes get real
  structured transcripts instead of an ANSI ``capture-pane`` fallback.

Because the remote side may run an older interpreter, this module deliberately
avoids type annotations, ``from __future__ import annotations`` and 3.8+ syntax
(walrus, dict union, positional-only markers). Keep it dependency-free: only
the standard library and ``tmux`` itself.
"""
from collections import deque
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

PROVIDERS = ("claude", "codex", "hermes", "kimi")
CLAUDE_SKIP_PREFIXES = ("<task-notification>", "<local-command-")
CODEX_SKIP_PREFIXES = (
    "<environment_context>", "<skills_instructions>", "<permissions instructions>",
    "<model_switch>", "<collaboration_mode>",
)
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PANED_ID_RE = re.compile(r"^%[0-9]+$")


class Adapter(object):
    """一个 agent CLI 在"读 transcript"这件事上的全部差异。

    这个模块会被 ``python3 -`` 管道到远端主机执行，所以不能用 dataclass /
    类型注解：保持一个 __slots__ 的普通类，3.6 也能跑。

    字段就是声明：怎么找进程、怎么读、怎么解析远端 tail、模型写在哪。
    新增一个 CLI = 末尾 register 一个 Adapter，下面没有一处 if/elif 需要改。
    """
    __slots__ = ("name", "read", "parse", "command_prefixes", "argv_names",
                 "payload_parsers", "remote_reversed", "takes_title", "takes_hint",
                 "takes_screen", "model_source", "note")

    def __init__(self, name, read, parse, command_prefixes=(), argv_names=(),
                 payload_parsers=None, remote_reversed=False, takes_title=True,
                 takes_hint=False, takes_screen=False, model_source="tail", note=""):
        self.name = name
        self.read = read
        self.parse = parse
        self.command_prefixes = tuple(command_prefixes) or (name,)
        self.argv_names = tuple(argv_names) or (name,)
        self.payload_parsers = dict(payload_parsers or {})
        self.remote_reversed = bool(remote_reversed)
        self.takes_title = bool(takes_title)
        self.takes_hint = bool(takes_hint)
        self.takes_screen = bool(takes_screen)
        self.model_source = model_source
        self.note = note

    def summary(self):
        """给 /api/transcript/adapters 用的形状（纯数据）。"""
        return {
            "name": self.name,
            "command_prefixes": list(self.command_prefixes),
            "argv_names": list(self.argv_names),
            "remote_reversed": self.remote_reversed,
            "payload_formats": sorted(self.payload_parsers) or [],
            "model_source": self.model_source,
            "note": self.note,
        }


ADAPTERS = {}


def register(adapter, replace=False):
    """装一个 adapter。重名要显式 replace，免得两个模块悄悄互相覆盖。"""
    if not isinstance(adapter, Adapter):
        raise TypeError("register() takes an Adapter")
    if adapter.name in ADAPTERS and not replace:
        raise ValueError("adapter already registered: %s" % adapter.name)
    ADAPTERS[adapter.name] = adapter
    return adapter


def adapters():
    """注册顺序（也决定了 provider_from_argv 的优先顺序）。"""
    return list(ADAPTERS.values())


def agent_names():
    names = []
    for adapter in adapters():
        for name in adapter.argv_names:
            if name not in names:
                names.append(name)
    return tuple(names)


def adapter_for(name):
    adapter = ADAPTERS.get((name or "").strip().lower())
    if adapter is None:
        raise TranscriptError("no adapter for provider %s" % (name or ""))
    return adapter


class TranscriptError(RuntimeError):
    pass


def provider_for_command(command):
    """Map a pane's foreground command to a transcript adapter (or None).

    Looks at ``ADAPTERS`` — adding a CLI is one registration, not a new branch
    in this chain. Hermes also matches a plain ``python`` interpreter because
    both expose their history through ``~/.hermes/state.db``.
    """
    command = (command or "").strip().lower()
    for adapter in adapters():
        for prefix in adapter.command_prefixes:
            if command.startswith(prefix):
                return adapter.name
    return None


def parse_process_table(raw):
    """pid -> (ppid, argv) 的进程表；ps 行格式 ``pid ppid args...``。"""
    table = {}
    for line in (raw or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, argv = parts
        if pid.isdigit() and ppid.isdigit():
            table[pid] = (ppid, argv)
    return table


def provider_for_pane(pane, table=None):
    """哪个 agent CLI 在这个 pane 里跑。

    只看 pane_current_command 不够：Codex / Claude 有时是被 shell 包着启动的
    （`zsh -c 'codex …'`、后台进程、runtime 外壳），tmux 报的前台命令就退化成
    `zsh`/`bash`，于是控制台拿不到 transcript，对话视图是空的。
    退路是看进程树：pane_pid 的后代里谁叫 codex/claude/kimi/hermes。
    """
    provider = provider_for_command(pane.get("command"))
    if provider:
        return provider
    root = str(pane.get("pane_pid") or "").strip()
    if table and root.isdigit() and root in table:
        children = {}
        for pid, (ppid, argv) in table.items():
            children.setdefault(ppid, []).append((pid, argv))
        seen = set()
        queue = [(child, 1) for child in children.get(root, [])]
        while queue:
            (pid, argv), depth = queue.pop(0)
            if pid in seen or depth > 6:
                continue
            seen.add(pid)
            found = provider_from_argv(argv)
            if found:
                return found
            queue.extend((child, depth + 1) for child in children.get(pid, []))
    # 不看窗口/会话名：名字里带 codex 但实际是普通 shell 的情况太多（误判会让
    # 对话视图去读不存在的东西），只认真正在跑的进程。
    return None


def _provider_for_argv_name(name):
    """argv 里那个可执行名 -> adapter 名字。

    名字不总等于 provider：``kimi-code`` 是 kimi，``fakecli`` 也可能是 ``fake``；
    所以这里比对的是 adapter 声明的 argv_names，返回的是 adapter.name。
    """
    for adapter in adapters():
        for token in adapter.argv_names:
            if name == token or name.startswith(token + ".") or name.startswith(token + "-"):
                return adapter.name
    return None


def provider_from_argv(argv):
    """一条 argv 是哪个 agent：``codex``、``/x/bin/codex``、``node .../@openai/codex/bin/codex.js`` 都算。"""
    found = _provider_for_argv_name(app_name(argv))
    if found:
        return found
    for token in str(argv or "").split():
        base = os.path.basename(token).lower()
        if not base or base.startswith("-"):
            continue
        # kimi-code / hermes-agent / claude-code：包一层壳也算同一个 agent
        found = _provider_for_argv_name(base)
        if found:
            return found
    return None


def app_name(argv):
    """``/opt/homebrew/bin/codex --flag`` → ``codex``。"""
    if not argv:
        return ""
    first = str(argv).strip().split(None, 1)[0]
    return os.path.basename(first).lower()


def limit_cap(limit, ceiling=None):
    """Clamp a requested message count to the configured maximum."""
    if ceiling is None:
        try:
            ceiling = int(os.environ.get("TMUX_WEB_TRANSCRIPT_LIMIT_MAX", "3000"))
        except ValueError:
            ceiling = 3000
    return max(1, min(int(limit), max(1, ceiling)))


def reverse_lines(path, chunk_size=64 * 1024, max_line=8 * 1024 * 1024):
    """Yield non-empty lines from the end of a file without reading all of it."""
    with Path(path).open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            size = min(chunk_size, position)
            position -= size
            handle.seek(position)
            block = handle.read(size) + remainder
            parts = block.split(b"\n")
            remainder = parts[0]
            if len(remainder) > max_line:
                remainder = b""
            for raw in reversed(parts[1:]):
                if raw and len(raw) <= max_line:
                    yield raw.decode(errors="replace")
        if remainder and len(remainder) <= max_line:
            yield remainder.decode(errors="replace")


def file_contains(path, needle):
    target = needle.encode()
    overlap = max(0, len(target) - 1)
    previous = b""
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(64 * 1024)
            if not block:
                return False
            data = previous + block
            if target in data:
                return True
            previous = data[-overlap:] if overlap else b""


def content_text(content, allowed_types):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


MODEL_RE = None


def tail_model(path, tail: int = 262144) -> str:
    """最后一条记录里的 ``"model": "…"``。

    Every agent records the model it used in its own session file (claude jsonl,
    codex rollout, kimi wire, hermes state.db); the model picker only needs the
    newest one, so a tail read is enough and costs nothing next to the transcript.
    """
    global MODEL_RE
    if MODEL_RE is None:
        import re as _re
        MODEL_RE = _re.compile(r'"model"\s*:\s*"([^"]{1,80})"')
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - tail))
            chunk = handle.read().decode("utf-8", "replace")
        found = MODEL_RE.findall(chunk)
        if not found:
            # 尾部可能全是工具输出（没有 "model" 字段），退回读全文；只在一次
            # 取不到时才付这个代价，且文件通常几十 MB 以内
            with open(path, "rb") as handle:
                found = MODEL_RE.findall(handle.read().decode("utf-8", "replace"))
    except OSError:
        return ""
    return found[-1].strip() if found else ""


def _mtime(path):
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


def _pane_env(pane):
    """Return (command, cwd, title, pane_pid) for a tmux pane."""
    if not PANED_ID_RE.match(pane or ""):
        raise TranscriptError("invalid pane id")
    completed = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane,
         "#{pane_current_command}\t#{pane_current_path}\t#{pane_title}\t#{pane_pid}"],
        capture_output=True, check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise TranscriptError(detail or "tmux display-message failed")
    fields = completed.stdout.decode(errors="replace").rstrip("\n").split("\t", 3)
    while len(fields) < 4:
        fields.append("")
    return fields[0], fields[1], fields[2], fields[3]


def resume_session_hint(pane_pid, proc_root=None):
    """Find a ``--resume <id>`` flag in the pane's process tree."""
    proc_root = Path(proc_root) if proc_root is not None else Path("/proc")
    try:
        queue = [int(pane_pid)]
    except (TypeError, ValueError):
        return None
    seen = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            cmdline = (proc_root / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            cmdline = ""
        match = re.search(r"--resume\s+([0-9A-Za-z][0-9A-Za-z_-]{7,63})", cmdline)
        if match is None:
            match = re.search(r"(?:^|\s)-r\s+([0-9a-f-]{36})", cmdline)
        if match is None:
            # `codex resume <id>` is a subcommand, not a flag
            match = re.search(r"(?:^|\s)resume\s+([0-9A-Za-z][0-9A-Za-z_-]{7,63})", cmdline)
        if match:
            return match.group(1)
        try:
            children = subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, check=False
            ).stdout.split()
        except OSError:
            children = []
        for child in children:
            try:
                queue.append(int(child))
            except ValueError:
                continue
    return None


def _claude_session_path(home, cwd, title, screen, hint):
    projects = Path(home) / ".claude" / "projects"
    if hint:
        matches = sorted(projects.glob("*/%s.jsonl" % hint))
        if matches:
            return matches[0]
    project_dir = projects / re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
    candidates = list(project_dir.glob("*.jsonl")) if project_dir.is_dir() else []
    title = re.sub(r"^[^A-Za-z0-9\u4e00-\u9fff]+", "", title or "").strip()
    if title:
        title_matches = []
        for path in candidates:
            try:
                if file_contains(path, title):
                    title_matches.append(path)
            except OSError:
                continue
        if len(title_matches) == 1:
            return title_matches[0]
    # Only capture the pane when the cheap probes failed.
    text = screen() if callable(screen) else (screen or "")
    probes = [line.strip() for line in str(text).splitlines() if len(line.strip()) >= 24]
    for probe in reversed(probes):
        matches = []
        for path in candidates:
            try:
                if file_contains(path, probe):
                    matches.append(path)
            except OSError:
                continue
        if len(matches) == 1:
            return matches[0]
    raise TranscriptError("could not determine Claude session id")


def question_record(call_id, arguments, timestamp):
    """A recorded question is history. It carries no authority to answer a live request."""
    if not isinstance(call_id, str) or not call_id:
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    questions = arguments.get("questions") if isinstance(arguments, dict) else None
    if not isinstance(questions, list):
        return None
    lines = []
    for item in questions[:8]:
        if not isinstance(item, dict):
            continue
        question = item.get("question") or item.get("title")
        if not isinstance(question, str) or not question.strip():
            continue
        mode = "多选" if item.get("multiSelect") else "选择" if item.get("options") else "填写"
        lines.append("【" + mode + "】" + question[:480])
        options = item.get("options")
        if not isinstance(options, list):
            continue
        for opt in options[:12]:
            if isinstance(opt, str):
                lines.append("• " + opt[:160])
                continue
            if not isinstance(opt, dict) or not isinstance(opt.get("label"), str):
                continue
            desc = opt.get("description")
            lines.append("• " + opt["label"][:160] + (" — " + desc[:240] if isinstance(desc, str) and desc else ""))
    if lines:
        return {"id": "question:" + call_id, "role": "interaction",
                "text": "\n".join(lines)[:4800], "timestamp": timestamp}
    return None


def parse_claude(lines, limit, session_id="", mtime=0):
    """Parse Claude Code JSONL. ``lines`` must be newest-first."""
    max_records = limit_cap(limit)
    records = []
    seen = set()
    has_more = False
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        role = event.get("type")
        uuid = event.get("uuid")
        if event.get("isCompactSummary"):
            continue
        if role not in ("user", "assistant") or not uuid or uuid in seen:
            continue
        content = (event.get("message") or {}).get("content")
        batch = []
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                    record = question_record(block.get("id"), block.get("input"), event.get("timestamp"))
                    if record and record["id"] not in seen:
                        seen.add(record["id"])
                        batch.append(record)
        text = content_text(content, ("text",))
        if text and not text.startswith(CLAUDE_SKIP_PREFIXES):
            batch.insert(0, {"id": uuid, "role": role, "text": text, "timestamp": event.get("timestamp")})
        if batch:
            seen.add(uuid)
            records.extend(reversed(batch))
        if len(records) >= max_records:
            has_more = True
            break
    records = records[:max_records]
    records.reverse()
    return {"provider": "claude", "session_id": session_id, "messages": records,
            "mtime": mtime, "has_more": has_more}


def read_claude(home, cwd, title, limit, hint=None, screen=None):
    path = _claude_session_path(home, cwd, title, screen, hint)
    data = parse_claude(reverse_lines(path), limit, path.stem, _mtime(path))
    data["path"] = str(path)
    return data


def _codex_session_path(home, cwd, title, hint=None):
    codex_home = Path(home) / ".codex"
    title = (title or "").split(" | ", 1)[0].strip()
    session_id = None
    index_path = codex_home / "session_index.jsonl"
    if index_path.exists() and title:
        for line in index_path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("thread_name") == title:
                session_id = entry.get("id")
    files = list((codex_home / "sessions").rglob("*.jsonl"))
    if hint:
        # the pane's own argv beats both the title index and the cwd guess
        matches = [path for path in files if str(hint) in path.name]
        if matches:
            return str(hint), max(matches, key=_mtime)
    if session_id:
        matches = [path for path in files if session_id in path.name]
        if matches:
            return str(session_id), max(matches, key=_mtime)
    # Newest-first so the first cwd match is the same session the scan-heavy
    # implementation would have selected, without reading every session file.
    for path in sorted(files, key=_mtime, reverse=True):
        try:
            with path.open(errors="replace") as handle:
                first = json.loads(handle.readline())
        except (OSError, ValueError):
            continue
        payload = first.get("payload") or {}
        if first.get("type") == "session_meta" and payload.get("cwd") == cwd:
            return str(payload.get("id") or payload.get("session_id")), path
    raise TranscriptError("Codex transcript not found")


def parse_codex(lines, limit, session_id="", mtime=0):
    """Parse Codex rollout JSONL (oldest-first input)."""
    cap = limit_cap(limit)
    records = deque(maxlen=cap + 1)
    seen = set()
    model = ""
    pending = {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "event_msg":
            marker = event.get("payload")
            if isinstance(marker, dict) and marker.get("type") in ("task_started", "task_complete", "turn_aborted"):
                pending.clear()
        if event.get("type") == "turn_context":
            context = event.get("payload")
            if isinstance(context, dict) and isinstance(context.get("model"), str):
                model = context["model"]
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "function_call" and payload.get("name") in (
                "request_user_input", "request_user_input_async", "functions.request_user_input",
                "functions.request_user_input_async"):
            record = question_record(payload.get("call_id"), payload.get("arguments"), event.get("timestamp"))
            if record and record["id"] not in seen:
                seen.add(record["id"])
                records.append(record)
                if payload["name"].split(".")[-1] == "request_user_input":
                    pending[payload["call_id"]] = record
        elif payload.get("type") == "function_call_output" and isinstance(payload.get("call_id"), str):
            pending.pop(payload["call_id"], None)
        if payload.get("type") != "message" or payload.get("role") not in ("user", "assistant"):
            continue
        message_id = payload.get("id")
        text = content_text(payload.get("content"), ("input_text", "output_text", "text"))
        if not text or text.startswith(CODEX_SKIP_PREFIXES):
            continue
        if not message_id:
            identity = [session_id, event.get("timestamp"), payload["role"], text]
            message_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if message_id in seen:
            continue
        seen.add(message_id)
        records.append({"id": message_id, "role": payload["role"], "text": text,
                        "timestamp": event.get("timestamp")})
    return {"provider": "codex", "session_id": session_id, "model": model,
            "open_questions": [{"id": item["id"], "text": item["text"]} for item in pending.values()],
            "messages": list(records)[-cap:], "mtime": mtime, "has_more": len(records) > cap}


def read_codex(home, cwd, title, limit, hint=None):
    session_id, path = _codex_session_path(home, cwd, title, hint)
    with path.open(errors="replace") as handle:
        data = parse_codex(handle, limit, session_id, _mtime(path))
    if not hint or str(hint) != session_id or not (path.stem == hint or path.stem.endswith("-" + str(hint))):
        # The title/cwd fallback can locate a sibling. Only a pane's own
        # resume argument can turn historical records into a pending notice.
        data["open_questions"] = []
    data["path"] = str(path)
    return data


def _kimi_session_path(home, cwd, title):
    sessions_root = Path(home) / ".kimi-code" / "sessions"
    candidates = []
    for state_path in sessions_root.glob("wd_*/session_*/state.json"):
        try:
            state = json.loads(state_path.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        if state.get("archived") or state.get("cwd") != cwd:
            continue
        wire_path = state_path.parent / "agents" / "main" / "wire.jsonl"
        if not wire_path.is_file():
            continue
        stamp = state.get("updatedAt") or state.get("createdAt") or 0
        try:
            stamp = float(stamp) / 1000 if float(stamp) > 10**11 else float(stamp)
        except (TypeError, ValueError):
            stamp = 0.0
        candidates.append((state, wire_path, stamp))
    if not candidates:
        raise TranscriptError("could not determine Kimi session id")
    title = (title or "").strip()
    preferred = [item for item in candidates if item[0].get("title", "").strip() == title]
    # the live session is the newest one: several can share a cwd (and a title),
    # and the older ones end in an aborted turn with no messages
    pool = preferred or candidates
    pool.sort(key=lambda item: item[2], reverse=True)
    return pool[0][0], pool[0][1]


def _kimi_time(value):
    """kimi 写的是毫秒；老版本偶尔是秒或 ISO 串。"""
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            value = int(text)
        else:
            return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000 if number > 10**11 else number


def parse_kimi(lines, limit, session_id="", mtime=0):
    """Parse Kimi wire.jsonl. ``lines`` must be newest-first.

    Two layouts exist and both are still in the wild:

    * kimi 1.x — one ``agent.message.appended`` per chat message, with
      ``message.meta.source`` telling user input (``input``) from model output (``llm``).
    * kimi 2.x (2.3.3 here) — user turns land as ``context.append_message``; the
      assistant answer is streamed as ``context.append_loop_event`` events whose
      ``event.type`` is ``content.part`` and ``event.part.type`` is ``text`` (``think``
      parts are the model's private reasoning and are dropped).  Parts of one step
      belong to one answer, so they are joined back together.
    """
    max_records = limit_cap(limit)
    items: list[dict] = []
    seen: set[str] = set()
    has_more = False
    seq = 0

    def add(record):
        nonlocal seq
        record["_seq"] = seq
        seq += 1
        items.append(record)

    step_parts: dict[str, list[str]] = {}
    step_time: dict[str, float] = {}
    step_order: list[str] = []

    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "agent.message.appended":                       # kimi 1.x
            envelope = event.get("message") or {}
            message = envelope.get("message") or {}
            meta = envelope.get("meta") or {}
            role = message.get("role")
            source = meta.get("source")
            if role == "user" and source != "input":
                continue
            if role == "assistant" and source != "llm":
                continue
            if role not in ("user", "assistant"):
                continue
            text = content_text(message.get("content"), ("text",))
            if not text:
                continue
            message_id = meta.get("userMessageId") or meta.get("messageId")
            if not message_id:
                import hashlib
                message_id = hashlib.sha256(
                    ("%s:%s:%s" % (event.get("time"), role, text)).encode()).hexdigest()
            add({"id": str(message_id), "role": role, "text": text,
                 "timestamp": _kimi_time(event.get("time"))})
        elif kind == "context.append_message":                     # kimi 2.x user turn
            message = event.get("message") or {}
            role = message.get("role")
            origin = (message.get("origin") or {}).get("kind")
            if role not in ("user", "assistant"):
                continue
            if role == "user" and origin and origin != "user":
                continue
            text = content_text(message.get("content"), ("text",))
            if not text:
                continue
            message_id = message.get("id") or message.get("promptId") or ""
            if not message_id:
                import hashlib
                message_id = hashlib.sha256(
                    ("%s:%s" % (event.get("time"), text)).encode()).hexdigest()
            add({"id": str(message_id), "role": role, "text": text,
                 "timestamp": _kimi_time(event.get("time"))})
        elif kind == "context.append_loop_event":                  # kimi 2.x assistant stream
            inner = event.get("event") or {}
            if inner.get("type") != "content.part":
                continue
            part = inner.get("part") or {}
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            step_id = str(inner.get("stepUuid") or "%s:%s" % (inner.get("turnId"), inner.get("step")))
            if step_id not in step_parts:
                step_parts[step_id] = []
                step_order.append(step_id)
                step_time[step_id] = _kimi_time(event.get("time"))
            step_parts[step_id].append(text)
        else:
            continue
        if len(items) + len(step_parts) >= max_records:
            break

    for step_id in step_order:
        text = "\n\n".join(part.strip() for part in reversed(step_parts[step_id]) if part.strip()).strip()
        if not text:
            continue
        add({"id": step_id, "role": "assistant", "text": text, "timestamp": step_time.get(step_id, 0.0)})

    items.sort(key=lambda record: (record.get("timestamp") or 0.0, -record["_seq"]), reverse=True)
    records = []
    for record in items:
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        record.pop("_seq", None)
        records.append(record)
        if len(records) >= max_records:
            has_more = True
            break
    records.reverse()
    return {"provider": "kimi", "session_id": session_id, "messages": records,
            "mtime": mtime, "has_more": has_more}


def read_kimi(home, cwd, title, limit):
    state, path = _kimi_session_path(home, cwd, title)
    session_id = str(state.get("id") or path.parents[1].name)
    data = parse_kimi(reverse_lines(path), limit, session_id, _mtime(path))
    data["path"] = str(path)
    return data


def _hermes_session(connection, cwd, hint):
    """Pick the Hermes session that belongs to a pane.

    Sessions come from the CLI, the local/web gateway and one-shot runs, and a
    bare cwd lookup can land on a freshly created empty row. Prefer the session
    the pane was resumed from, then the most recently active session that
    actually contains messages.
    """
    if hint:
        row = connection.execute("SELECT id FROM sessions WHERE id = ?", (hint,)).fetchone()
        if row is not None:
            return row
    query = (
        "SELECT s.id AS id FROM sessions s WHERE s.cwd = ? "
        "ORDER BY (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND m.active = 1 "
        "          AND COALESCE(m.content, '') <> '') = 0, "
        "         COALESCE(s.last_activity_at, s.started_at) DESC LIMIT 1"
    )
    try:
        return connection.execute(query, (cwd,)).fetchone()
    except sqlite3.OperationalError:
        # Older Hermes schemas may not have every column this ordering touches.
        return connection.execute(
            "SELECT id FROM sessions WHERE cwd = ? "
            "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1",
            (cwd,),
        ).fetchone()


def read_hermes(home, cwd, limit, hint=None):
    db_path = Path(home) / ".hermes" / "state.db"
    if not db_path.exists():
        raise TranscriptError("Hermes state database not found")
    with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        session = _hermes_session(connection, cwd, hint)
        if session is None:
            raise TranscriptError("Hermes session not found")
        session_id = session["id"]
        rows = connection.execute(
            "SELECT id, role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 "
            "AND role IN ('user','assistant') AND COALESCE(content,'') <> '' ORDER BY id DESC LIMIT ?",
            (session_id, max(1, min(int(limit), 1000))),
        ).fetchall()
    return parse_hermes_rows([dict(row) for row in rows], limit, session_id, _mtime(db_path))


def parse_hermes_rows(rows, limit, session_id="", mtime=0):
    """Rows from state.db messages (id, role, content, timestamp), newest first."""
    messages = [
        {"id": str(row.get("id")), "role": row.get("role"), "text": row.get("content"),
         "timestamp": row.get("timestamp")}
        for row in reversed(rows)
        if (row.get("content") or "").strip()
    ]
    cap = limit_cap(limit)
    return {"provider": "hermes", "session_id": session_id,
            "messages": messages[-cap:], "mtime": mtime, "has_more": len(messages) > cap}


def parse_hermes_session(lines, limit, session_id="", mtime=0):
    """~/.hermes/sessions/<id>.jsonl: {"role","content","timestamp"} per line."""
    messages = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        role = event.get("role")
        if role not in ("user", "assistant"):
            continue
        text = event.get("content")
        if isinstance(text, list):
            text = content_text(text, ("text",))
        if not isinstance(text, str) or not text.strip():
            continue
        messages.append({"id": "%s-%d" % (session_id, len(messages)), "role": role,
                         "text": text.strip(), "timestamp": event.get("timestamp")})
    cap = limit_cap(limit)
    return {"provider": "hermes", "session_id": session_id,
            "messages": messages[-cap:], "mtime": mtime, "has_more": len(messages) > cap}


def parse_remote_payload(provider, payload_format, payload, limit, session_id="", mtime=0):
    """Parse bytes pulled from a remote host with the same code paths as local files.

    命名格式（hermes-rows 之类整段 JSON）先查 adapter 的 payload_parsers；
    其余一律按行交给 adapter.parse，要不要反转由 adapter 的 remote_reversed 决定
    —— 这几个解析器对行序的要求不一样，写死一个方向必然错一半。
    """
    adapter = adapter_for(provider)
    lines = payload.splitlines()
    if payload_format:
        parser = adapter.payload_parsers.get(payload_format)
        if parser is not None:
            # 命名格式的 parser 也吃行列表：hermes-jsonl 是逐行 JSON，
            # hermes-rows 是整段 JSON 数组（自己再拼回去），契约只有一种。
            return parser(lines, limit, session_id, mtime)
        if payload_format in _special_payload_formats():
            # 别的 provider 专属格式跑到这里来了，说明调用方认错了对象 —— 别硬解析
            raise TranscriptError("no parser for provider %s / format %s"
                                  % (provider, payload_format))
        # 普通格式（jsonl/plain）：按 provider 的默认解析器走，和以前一致
    if adapter.remote_reversed:
        lines = list(reversed(lines))
    return adapter.parse(lines, limit, session_id, mtime)


def _special_payload_formats():
    formats = set()
    for adapter in adapters():
        formats.update(adapter.payload_parsers)
    return formats


def _parse_hermes_rows_payload(lines, limit, session_id, mtime):
    payload = "\n".join(lines)
    try:
        rows = json.loads(payload) if payload.strip() else []
    except ValueError as exc:
        raise TranscriptError("remote message rows are not valid JSON") from exc
    if not isinstance(rows, list):
        raise TranscriptError("remote message rows have an unexpected shape")
    return parse_hermes_rows(rows, limit, session_id, mtime)


register(Adapter(
    "claude", read_claude, parse_claude,
    command_prefixes=("claude",), argv_names=("claude",),
    remote_reversed=True, takes_hint=True, takes_screen=True,
    note="~/.claude/projects/*/*.jsonl，按进程树/screen 里的 session id 定位"))
register(Adapter(
    "codex", read_codex, parse_codex,
    command_prefixes=("codex",), argv_names=("codex",),
    remote_reversed=False, takes_hint=True, takes_screen=False,
    note="~/.codex/sessions/**/rollout-*.jsonl，远端 tail 本来就是 oldest-first"))
register(Adapter(
    "kimi", read_kimi, parse_kimi,
    command_prefixes=("kimi",), argv_names=("kimi",),
    remote_reversed=True, takes_hint=False, takes_screen=False,
    note="~/.kimi/sessions/**/wire.jsonl，解析器吃 newest-first"))
register(Adapter(
    "hermes", read_hermes, parse_hermes_session,
    command_prefixes=("hermes", "python"), argv_names=("hermes",),
    payload_parsers={"hermes-rows": _parse_hermes_rows_payload,
                     "hermes-jsonl": parse_hermes_session},
    remote_reversed=False, takes_title=False, takes_hint=True, takes_screen=False,
    model_source="sqlite",
    note="~/.hermes/state.db（sqlite 优先，单会话 jsonl 兜底）"))

# 老名字保留：外部（tmux_web / 测试 / 远端脚本）还在用
READERS = {adapter.name: adapter.read for adapter in adapters()}
PROVIDERS = tuple(adapter.name for adapter in adapters())
AGENT_NAMES = agent_names()


def read_transcript(provider, cwd, title, limit=200, home=None, hint=None, screen=None):
    """Read a provider transcript for one pane.

    Everything provider-specific comes from the adapter: which function reads,
    whether it can use a session-id hint, and where the model name lives.
    """
    try:
        adapter = adapter_for(provider)
    except TranscriptError:
        raise TranscriptError("structured transcript is not supported for this pane")
    home = Path(home) if home is not None else Path.home()
    kwargs = {}
    if adapter.takes_hint:
        kwargs["hint"] = hint
    if adapter.takes_screen:
        kwargs["screen"] = screen
    args = [home, cwd] + ([title] if adapter.takes_title else []) + [limit]
    data = adapter.read(*args, **kwargs)
    if not data.get("messages"):
        raise TranscriptError("this session has no messages yet")
    if not data.get("model"):
        filled = ""
        primary = data.get("path") or ""
        if primary and provider != "codex":
            filled = tail_model(primary)
        if not filled and adapter.model_source == "sqlite":
            filled = hermes_model(Path(home), data.get("session_id") or "")
        data["model"] = filled
    return data


def hermes_model(home, session_id) -> str:
    """Hermes keeps the model on the session row, not in the messages."""
    if not session_id:
        return ""
    db_path = Path(home) / ".hermes" / "state.db"
    try:
        with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5) as connection:
            row = connection.execute("select model from sessions where id = ?", (session_id,)).fetchone()
    except sqlite3.Error:
        return ""
    return (row[0] or "").strip() if row else ""


def pane_screen(pane, lines=80):
    completed = subprocess.run(
        ["tmux", "capture-pane", "-p", "-J", "-t", pane, "-S", "-%d" % max(20, int(lines))],
        capture_output=True, check=False,
    )
    if completed.returncode != 0:
        return ""
    return ANSI_RE.sub("", completed.stdout.decode(errors="replace"))


def main(argv):
    """CLI used over SSH: read the transcript of a remote pane as JSON."""
    import argparse

    parser = argparse.ArgumentParser(description="Read an agent CLI transcript for a tmux pane")
    parser.add_argument("--pane")
    parser.add_argument("--command")
    parser.add_argument("--cwd")
    parser.add_argument("--title")
    parser.add_argument("--pane-pid")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--provider")
    args = parser.parse_args(argv)

    command, cwd, title, pane_pid = args.command, args.cwd, args.title, args.pane_pid
    if args.pane:
        try:
            command, cwd, title, pane_pid = _pane_env(args.pane)
        except TranscriptError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
    provider = args.provider or provider_for_command(command)
    payload = {"ok": False, "provider": provider, "command": command}
    try:
        if provider == "claude":
            hint = resume_session_hint(pane_pid) if pane_pid else None
            payload["transcript"] = read_claude(
                Path.home(), cwd, title, args.limit, hint=hint,
                screen=pane_screen(args.pane) if args.pane else None,
            )
            if not payload["transcript"].get("messages"):
                raise TranscriptError("this session has no messages yet")
        elif provider == "hermes":
            hint = resume_session_hint(pane_pid) if pane_pid else None
            payload["transcript"] = read_hermes(Path.home(), cwd, args.limit, hint=hint)
            if not payload["transcript"].get("messages"):
                raise TranscriptError("this session has no messages yet")
        elif provider is None:
            raise TranscriptError("structured transcript is not supported for this pane")
        else:
            payload["transcript"] = read_transcript(provider, cwd, title, args.limit)
        payload["ok"] = True
    except (TranscriptError, OSError, ValueError, sqlite3.Error) as exc:
        payload["error"] = str(exc)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
