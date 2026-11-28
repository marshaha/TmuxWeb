"""Claude Code harness facts.

Single source of truth for everything TmuxWeb knows about the Claude
Code CLI: how to spot it, where its session file lives, which records
mean idle/busy, which tool calls can be outstanding, which hook events
mean waiting, and how to resume it.

The functions in this module are written in conservative, stdlib-only
Python (no 3.9+ syntax): the generator
(``scripts/generate_agent_provider_facts.py``) copies them verbatim
into the agent program (``plugins/agent/agent.py``), which is deployed
as one file and runs on hosts with older interpreters. Keep them that
way — the drift test fails if the agent copy disagrees.
"""
import json
import os
import re
from pathlib import Path

NAME = "claude"
LABEL = "Claude Code"
BINS = ("claude",)
COMMAND_PREFIXES = ("claude",)
MODEL_PRESETS = ("opus", "sonnet", "haiku")
MODEL_SWITCH_TEMPLATE = "/model {model}"
PROVIDER_LABEL = "Anthropic"
HOOK_TEMPLATE = "claude-settings.json"
# Raw event names as configured in the hook template: Notification with
# matcher permission_prompt|elicitation_dialog sets waiting; Stop clears.
HOOK_WAITING = ("permission_prompt", "elicitation_dialog")
HOOK_CLEARING = ("stop",)
GATED = True
IDLE_MARKERS = ('"subtype":"turn_duration"', '"subtype":"stop_hook_summary"')
BUSY_MARKERS = ('"type":"user"', '"type":"assistant"')
# Resume flags, interface only (PR #23 plugs in here later): `claude
# --resume <id>` — the same form resume_session_hint already parses
# out of real process trees in transcript_reader.py.
RESUME_TEMPLATE = ("--resume", "{session_id}")
TRANSCRIPT_SOURCE = (
    {"kind": "jsonl", "root": "~/.claude/projects",
     "layout": "<dashified-cwd>/*.jsonl", "hint": "*/<session_id>.jsonl"},
)


def claude_file_contains(path, needle):
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


def claude_outstanding(records):
    """A tool_use id with no tool_result yet. A plain user message after the
    call means the user intervened and the call is abandoned; a turn-end
    marker closes everything before it."""
    pending = set()
    for event in records:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "assistant":
            content = (event.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("id")):
                        pending.add(block["id"])
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            results = [block for block in blocks
                       if isinstance(block, dict) and block.get("type") == "tool_result"]
            if results:
                for block in results:
                    pending.discard(block.get("tool_use_id"))
            elif blocks or isinstance(content, str):
                pending.clear()
        elif kind == "system" and event.get("subtype") in ("turn_duration", "stop_hook_summary"):
            pending.clear()
    return bool(pending)


def claude_resolve_session_file(home, cwd, title, screen=None, hint=None):
    """Locate a Claude session file, or None.

    Same rules as transcript_reader._claude_session_path, plus the
    newest-file fallback agent_status used to apply when that raised:
    hint glob first, then a session whose bytes contain the pane title,
    then one whose bytes contain a long on-screen line, then the newest
    file in this cwd's project directory.
    """
    home = Path(home) if home is not None else Path.home()
    projects = home / ".claude" / "projects"
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
                if claude_file_contains(path, title):
                    title_matches.append(path)
            except OSError:
                continue
        if len(title_matches) == 1:
            return title_matches[0]
    text = screen() if callable(screen) else (screen or "")
    probes = [line.strip() for line in str(text).splitlines() if len(line.strip()) >= 24]
    for probe in reversed(probes):
        matches = []
        for path in candidates:
            try:
                if claude_file_contains(path, probe):
                    matches.append(path)
            except OSError:
                continue
        if len(matches) == 1:
            return matches[0]
    if candidates:
        try:
            return max(candidates, key=lambda item: item.stat().st_mtime)
        except OSError:
            return None
    return None


def native_form(transport, local, actual, pid, provider_for, submission):
    import chat_ipc
    if transport is not local:
        return {"status": "unavailable"}
    return chat_ipc.query(pid, actual, submission, provider="claude")
