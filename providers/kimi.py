"""Kimi Code harness facts.

Single source of truth for everything TmuxWeb knows about the Kimi
Code CLI. See providers/claude.py for the contract: the functions here
are conservative stdlib-only Python, copied verbatim into the agent
program by scripts/generate_agent_provider_facts.py.
"""
import json
import os
from pathlib import Path

NAME = "kimi"
LABEL = "Kimi Code"
BINS = ("kimi",)
COMMAND_PREFIXES = ("kimi",)
MODEL_PRESETS = ("K3.1 Pro", "K3", "K3-256k", "K2.7 Code Highspeed")
# Kimi only understands the argument-less form: `/model <anything>`
# answers "Unknown model alias", so the switch opens its own picker.
MODEL_SWITCH_TEMPLATE = "/model"
PROVIDER_LABEL = "Kimi Code"
HOOK_TEMPLATE = "kimi-hooks.toml"
# Raw event names as configured in the hook template: PermissionRequest
# sets waiting ("just before waiting for user approval");
# PermissionResult / Stop clear.
HOOK_WAITING = ("PermissionRequest",)
HOOK_CLEARING = ("PermissionResult", "Stop")
GATED = True
IDLE_MARKERS = ('"type":"prompt.completed"', '"type":"turn.ended"')
BUSY_MARKERS = ('"type":"prompt.submitted"', '"type":"llm.request"', '"type":"llm.response"',
                '"type":"step.start"', '"type":"step.end"', '"type":"tool.call"',
                '"type":"agent.message.appended"')
# Resume flags, interface only (PR #23 plugs in here later):
# `kimi --resume <id|index|latest>` (-r alias); --resume is a working
# hidden alias of --session, verified live on the server 2026-11-15.
RESUME_TEMPLATE = ("--resume", "{session_id}")
TRANSCRIPT_SOURCE = (
    {"kind": "jsonl", "root": "~/.kimi-code/sessions",
     "layout": "wd_*/session_*/state.json + agents/main/wire.jsonl"},
)


def kimi_outstanding(records):
    """Kimi has no turn marker for a wait either (state.json lastTurnReason is only
    ever 'completed'): the event stream is the signal. A tool.call opens a
    pending call; the step/turn ending, a new prompt, or the next llm.request
    (which only fires once the tool result is back) closes it."""
    pending = False
    for event in records:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "tool.call":
            pending = True
        elif kind in ("prompt.completed", "turn.ended", "step.end",
                      "prompt.submitted", "llm.request"):
            pending = False
        elif kind == "context.append_loop_event":
            if (event.get("event") or {}).get("type") == "step.end":
                pending = False
    return pending


def kimi_resolve_session_file(home, cwd, title, screen=None, hint=None):
    """Locate a Kimi wire.jsonl, or None.

    Same rules as transcript_reader._kimi_session_path: sessions with
    this cwd (not archived) whose wire file exists, title-matching ones
    preferred, newest updatedAt wins — several can share a cwd (and a
    title), and the older ones end in an aborted turn with no messages.
    """
    home = Path(home) if home is not None else Path.home()
    sessions_root = home / ".kimi-code" / "sessions"
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
        return None
    title = (title or "").strip()
    preferred = [item for item in candidates if item[0].get("title", "").strip() == title]
    # the live session is the newest one: several can share a cwd (and a title),
    # and the older ones end in an aborted turn with no messages
    pool = preferred or candidates
    pool.sort(key=lambda item: item[2], reverse=True)
    return pool[0][1]
