"""Codex harness facts.

Single source of truth for everything TmuxWeb knows about the Codex
CLI. See providers/claude.py for the contract: the functions here are
conservative stdlib-only Python, copied verbatim into the agent
program by scripts/generate_agent_provider_facts.py.
"""
import json
import os
from pathlib import Path

NAME = "codex"
LABEL = "Codex"
BINS = ("codex",)
COMMAND_PREFIXES = ("codex",)
MODEL_PRESETS = ()
MODEL_SWITCH_TEMPLATE = "/model {model}"
PROVIDER_LABEL = "OpenAI"
HOOK_TEMPLATE = "codex-hooks.json"
# Raw event names as configured in the hook template: PermissionRequest
# sets waiting; Stop clears. (Codex writes NO explicit approval-wait
# event in its rollout file — the permission lines there are approval
# policy settings, thread_settings_applied, and content mentions.)
HOOK_WAITING = ("PermissionRequest",)
HOOK_CLEARING = ("Stop",)
GATED = True
IDLE_MARKERS = ('"type":"task_complete"', '"type":"turn_aborted"')
BUSY_MARKERS = ('"type":"task_started"', '"type":"item_completed"', '"type":"token_count"')
# Resume flags, interface only (PR #23 plugs in here later): `codex
# resume <id>` is a subcommand, not a flag — the same form
# resume_session_hint already parses out of real process trees.
RESUME_TEMPLATE = ("resume", "{session_id}")
TRANSCRIPT_SOURCE = (
    {"kind": "jsonl", "root": "~/.codex/sessions", "layout": "**/*.jsonl",
     "index": "~/.codex/session_index.jsonl"},
)


def codex_mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def codex_outstanding(records):
    """A call item with no matching output item. Codex writes NO explicit
    approval-wait event: the permission/approval lines in a real rollout file
    are approval_policy settings, thread_settings_applied, and content mentions
    inside outputs — settings and quotes, not a wait event."""
    pending = set()
    for event in records:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "event_msg":
            marker = (event.get("payload") or {}).get("type")
            if marker in ("task_complete", "turn_aborted", "task_started"):
                pending.clear()
            continue
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload") or {}
        key = payload.get("call_id") or payload.get("id")
        if not key:
            continue
        if payload.get("type") in ("function_call", "custom_tool_call"):
            pending.add(key)
        elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
            pending.discard(key)
    return bool(pending)


def codex_resolve_session_file(home, cwd, title, screen=None, hint=None):
    """Locate a Codex rollout file, or None.

    Same rules as transcript_reader._codex_session_path: the pane's own
    argv hint wins, then the title index, then the newest file whose
    first record is a session_meta with this cwd — that first-record
    check is the genuinely code-shaped predicate (a bare glob match
    would land on a sibling session in the same directory).
    """
    home = Path(home) if home is not None else Path.home()
    codex_home = home / ".codex"
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
        matches = [path for path in files if str(hint) in path.name]
        if matches:
            return max(matches, key=codex_mtime)
    if session_id:
        matches = [path for path in files if session_id in path.name]
        if matches:
            return max(matches, key=codex_mtime)
    # Newest-first so the first cwd match is the same session the scan-heavy
    # implementation would have selected, without reading every session file.
    for path in sorted(files, key=codex_mtime, reverse=True):
        try:
            with path.open(errors="replace") as handle:
                first = json.loads(handle.readline())
        except (OSError, ValueError):
            continue
        payload = first.get("payload") or {}
        if first.get("type") == "session_meta" and payload.get("cwd") == cwd:
            return path
    return None


def native_form(transport, local, actual, pid, provider_for, submission):
    """Codex uses a local owner socket. A federation peer runs this on its own host."""
    import asyncio
    from pathlib import Path
    import codex_forms
    if transport is not local:
        return {"status": "unavailable"}

    def snapshot():
        pane = transport.validate_pane(actual)
        if str(pane.get("pane_pid")) != pid or provider_for(pane) != NAME:
            return "", "", ""
        return transport.capture(actual, 80, pane=pane, visible_only=True), pane.get("title", ""), pane.get("path", "")

    return asyncio.run(codex_forms.on_host(Path.home(), actual, pid, snapshot, submission))
