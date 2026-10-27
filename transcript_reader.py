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


class TranscriptError(RuntimeError):
    pass


def provider_for_command(command):
    """Map a pane's foreground command to a transcript provider (or None)."""
    command = (command or "").strip().lower()
    if command.startswith("claude"):
        return "claude"
    if command.startswith("codex"):
        return "codex"
    if command.startswith("kimi"):
        return "kimi"
    # Hermes runs as ``hermes`` or as a plain python interpreter; both expose
    # their history through ~/.hermes/state.db.
    if command.startswith("hermes") or command.startswith("python"):
        return "hermes"
    return None


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
        text = content_text((event.get("message") or {}).get("content"), ("text",))
        if not text or text.startswith(CLAUDE_SKIP_PREFIXES):
            continue
        seen.add(uuid)
        records.append({"id": uuid, "role": role, "text": text, "timestamp": event.get("timestamp")})
        if len(records) >= max_records:
            has_more = True
            break
    records.reverse()
    return {"provider": "claude", "session_id": session_id, "messages": records,
            "mtime": mtime, "has_more": has_more}


def read_claude(home, cwd, title, limit, hint=None, screen=None):
    path = _claude_session_path(home, cwd, title, screen, hint)
    return parse_claude(reverse_lines(path), limit, path.stem, _mtime(path))


def _codex_session_path(home, cwd, title):
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
    records = []
    seen = set()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload") or {}
        if payload.get("type") != "message" or payload.get("role") not in ("user", "assistant"):
            continue
        message_id = payload.get("id")
        if not message_id or message_id in seen:
            continue
        text = content_text(payload.get("content"), ("input_text", "output_text", "text"))
        if not text or text.startswith(CODEX_SKIP_PREFIXES):
            continue
        seen.add(message_id)
        records.append({"id": message_id, "role": payload["role"], "text": text,
                        "timestamp": event.get("timestamp")})
    cap = limit_cap(limit)
    return {"provider": "codex", "session_id": session_id,
            "messages": records[-cap:], "mtime": mtime, "has_more": len(records) > cap}


def read_codex(home, cwd, title, limit):
    session_id, path = _codex_session_path(home, cwd, title)
    with path.open(errors="replace") as handle:
        return parse_codex(handle, limit, session_id, _mtime(path))


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
        candidates.append((state, wire_path))
    title = (title or "").strip()
    title_matches = [item for item in candidates if item[0].get("title", "").strip() == title]
    if len(title_matches) == 1:
        return title_matches[0]
    if len(candidates) == 1:
        return candidates[0]
    raise TranscriptError("could not determine Kimi session id")


def parse_kimi(lines, limit, session_id="", mtime=0):
    """Parse Kimi wire.jsonl. ``lines`` must be newest-first."""
    max_records = limit_cap(limit)
    records = []
    seen = set()
    has_more = False
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "agent.message.appended":
            continue
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
            message_id = hashlib.sha256(("%s:%s:%s" % (event.get("time"), role, text)).encode()).hexdigest()
        if message_id in seen:
            continue
        seen.add(message_id)
        timestamp = event.get("time")
        if isinstance(timestamp, (int, float)):
            timestamp = timestamp / 1000
        records.append({"id": str(message_id), "role": role, "text": text, "timestamp": timestamp})
        if len(records) >= max_records:
            has_more = True
            break
    records.reverse()
    return {"provider": "kimi", "session_id": session_id, "messages": records,
            "mtime": mtime, "has_more": has_more}


def read_kimi(home, cwd, title, limit):
    state, path = _kimi_session_path(home, cwd, title)
    session_id = str(state.get("id") or path.parents[1].name)
    return parse_kimi(reverse_lines(path), limit, session_id, _mtime(path))


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
    """Parse bytes pulled from a remote host with the same code paths as local files."""
    lines = payload.splitlines()
    if payload_format == "hermes-rows":
        try:
            rows = json.loads(payload) if payload.strip() else []
        except ValueError as exc:
            raise TranscriptError("remote message rows are not valid JSON") from exc
        if not isinstance(rows, list):
            raise TranscriptError("remote message rows have an unexpected shape")
        return parse_hermes_rows(rows, limit, session_id, mtime)
    if payload_format == "hermes-jsonl":
        return parse_hermes_session(lines, limit, session_id, mtime)
    if provider in ("claude", "kimi"):
        # these readers expect newest-first input; remote tails arrive oldest-first
        return (parse_claude if provider == "claude" else parse_kimi)(
            list(reversed(lines)), limit, session_id, mtime)
    if provider == "codex":
        return parse_codex(lines, limit, session_id, mtime)
    raise TranscriptError("no parser for provider %s" % provider)


READERS = {"claude": read_claude, "codex": read_codex, "hermes": read_hermes, "kimi": read_kimi}


def read_transcript(provider, cwd, title, limit=200, home=None, hint=None, screen=None):
    """Read a provider transcript for one pane.

    ``hint`` is a Claude session id discovered from the process tree, ``screen``
    is optional pane text used as a last-resort session-id probe.
    """
    if provider not in READERS:
        raise TranscriptError("structured transcript is not supported for this pane")
    home = Path(home) if home is not None else Path.home()
    if provider == "claude":
        data = read_claude(home, cwd, title, limit, hint=hint, screen=screen)
    elif provider == "hermes":
        data = read_hermes(home, cwd, limit, hint=hint)
    else:
        data = READERS[provider](home, cwd, title, limit)
    if not data.get("messages"):
        raise TranscriptError("this session has no messages yet")
    return data


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
