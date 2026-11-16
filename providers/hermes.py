"""Hermes harness facts.

Single source of truth for everything TmuxWeb knows about the Hermes
CLI. Hermes is the odd one out: its transcript lives in a SQLite
database (``~/.hermes/state.db``, with a per-session JSONL fallback),
it has no usable wait signal in that state — its
``verification_required`` finish_reason appears twice in 43314 rows
and never as a session's latest message — and it has no user-facing
hook. So it is deliberately NOT gated: no idle/busy markers, no
outstanding predicate, no hook template, and the session-file
resolver returns None. That is fail-closed by design, not a gap.
"""
import os
from pathlib import Path

NAME = "hermes"
LABEL = "Hermes"
BINS = ("hermes",)
# Hermes runs as `hermes` or as a plain python interpreter; both expose
# their history through ~/.hermes/state.db.
COMMAND_PREFIXES = ("hermes", "python")
MODEL_PRESETS = ()
MODEL_SWITCH_TEMPLATE = "/model {model}"
PROVIDER_LABEL = "Hermes"
HOOK_TEMPLATE = ""
HOOK_WAITING = ()
HOOK_CLEARING = ()
GATED = False
IDLE_MARKERS = ()
BUSY_MARKERS = ()
# Resume flags, interface only (PR #23 plugs in here later):
# `hermes --resume <id>` — the same form resume_session_hint already
# parses out of real process trees.
RESUME_TEMPLATE = ("--resume", "{session_id}")
TRANSCRIPT_SOURCE = (
    {"kind": "sqlite", "db": "~/.hermes/state.db",
     "tables": {"sessions": "id, cwd, started_at, last_activity_at, model",
                "messages": "id, session_id, role, content, timestamp, active"}},
    {"kind": "jsonl", "root": "~/.hermes/sessions", "layout": "<session_id>.jsonl"},
)


def hermes_outstanding(records):
    """Hermes has no decision signal in its session file: never outstanding."""
    return False


def hermes_resolve_session_file(home, cwd, title, screen=None, hint=None):
    """No session *file* for Hermes: sessions live in state.db, so the
    file-based decision point cannot resolve one. Returns None."""
    return None
