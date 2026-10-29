"""Busy/idle from the agent's own session file.

Screen scraping guesses: a status bar that says "thinking: high" looks like a running
turn, and a TUI that redraws while idle looks busy. Every agent we support does record
the truth in its session file, at the end of the turn:

    claude  {"type":"system","subtype":"turn_duration",...}   <- turn finished
    codex   {"type":"event_msg","payload":{"type":"task_complete"}}
    kimi    {"type":"prompt.completed"}

so the last complete line is enough: it is one seek plus one read, no full parse.
"""

import json
import os
from pathlib import Path
import time

BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"

TAIL_BYTES = 65536

# A record that ends a turn (idle) versus one that belongs to a running turn (busy).
IDLE_MARKERS = {
    "claude": ('"subtype":"turn_duration"', '"subtype":"stop_hook_summary"'),
    "codex": ('"type":"task_complete"', '"type":"turn_aborted"'),
    "kimi": ('"type":"prompt.completed"', '"type":"turn.ended"'),
}
BUSY_MARKERS = {
    "claude": ('"type":"user"', '"type":"assistant"'),
    "codex": ('"type":"task_started"', '"type":"item_completed"', '"type":"token_count"'),
    "kimi": ('"type":"prompt.submitted"', '"type":"llm.request"', '"type":"llm.response"',
             '"type":"step.start"', '"type":"step.end"', '"type":"tool.call"',
             '"type":"agent.message.appended"'),
}


def last_complete_line(path, tail: int = TAIL_BYTES) -> str:
    """The newest line that is already complete (the last one may be mid-write)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - tail))
            chunk = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(chunk.splitlines()):
        stripped = line.strip()
        if stripped.endswith("}"):
            return stripped
    return ""


_TIGHTEN = __import__("re").compile(r'"[ \t]*:[ \t]*"')


def classify(provider: str, record: str) -> str:
    """Verdict for one session-file record (raw JSON text keeps both paths identical)."""
    provider = (provider or "").lower()
    if not record or provider not in IDLE_MARKERS:
        return UNKNOWN
    record = _TIGHTEN.sub('":"', record)      # tolerate '"type": "x"' as well as '"type":"x"' 
    if any(marker in record for marker in IDLE_MARKERS[provider]):
        return IDLE
    if any(marker in record for marker in BUSY_MARKERS[provider]):
        return BUSY
    return UNKNOWN


def classify_lines(provider: str, records) -> str:
    """Newest decisive record wins.

    Agents append bookkeeping after the turn boundary (codex writes
    "thread_settings_applied", claude writes system records), so only reading the very
    last line reports unknown for a pane that is plainly idle.
    """
    provider = (provider or "").lower()
    if provider not in IDLE_MARKERS:
        return UNKNOWN
    for record in reversed(list(records)):
        verdict = classify(provider, record)
        if verdict != UNKNOWN:
            return verdict
    return UNKNOWN


def tail_lines(path, tail: int = TAIL_BYTES):
    """Every complete line in the tail, oldest first."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - tail))
            chunk = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > tail and lines:
        lines = lines[1:]                 # the first line may start mid-record
    return [line.strip() for line in lines if line.strip().endswith("}")]


STALE_BUSY_SECONDS = int(os.environ.get("TMUXWEB_STATUS_STALE_SECONDS", "600"))


def status_for_file(provider: str, path) -> str:
    verdict = classify_lines(provider, tail_lines(path))
    if verdict == BUSY and STALE_BUSY_SECONDS > 0:
        # a running agent writes to its session file constantly; a file untouched for
        # minutes belongs to a dead or aborted turn (an aborted Kimi session ends on
        # agent.message.appended and would otherwise look busy forever)
        try:
            if time.time() - os.path.getmtime(path) > STALE_BUSY_SECONDS:
                return IDLE
        except OSError:
            return UNKNOWN
    return verdict


def status_for_pane(provider: str, cwd: str, title: str = "", home=None, screen=None) -> str:
    """Locate a pane's session file and read its verdict.

    Resolution is the same code the transcript reader uses, including the Kimi
    "newest updatedAt" rule, so status and transcript can never disagree about which
    session is live.
    """
    import transcript_reader

    home = home or os.path.expanduser("~")
    try:
        if provider == "claude":
            try:
                path = transcript_reader._claude_session_path(home, cwd, title, screen, None)
            except transcript_reader.TranscriptError:
                # several sessions, none attributable: the newest one is the live one
                import re
                project = (Path(home) / ".claude" / "projects"
                           / re.sub(r"[^A-Za-z0-9]", "-", cwd or ""))
                candidates = sorted(project.glob("*.jsonl"), key=lambda item: item.stat().st_mtime,
                                    reverse=True) if project.is_dir() else []
                if not candidates:
                    return UNKNOWN
                path = candidates[0]
            return status_for_file("claude", path)
        if provider == "codex":
            _, path = transcript_reader._codex_session_path(home, cwd, title)
            return status_for_file("codex", path)
        if provider == "kimi":
            _, path = transcript_reader._kimi_session_path(home, cwd, title)
            return status_for_file("kimi", path)
    except (OSError, ValueError, RuntimeError, transcript_reader.TranscriptError):
        return UNKNOWN
    return UNKNOWN


# The same rules in POSIX shell: remote hosts must not need python3.
STATUS_SHELL = r"""
status_of() {  # $1 = provider, $2 = session file -> busy|idle|unknown
  _st=unknown
  if [ -n "$2" ] && [ -f "$2" ]; then
    # newest decisive record: the last line that says either "turn ended" or "turn running"
    _last=$(tail -n 200 -- "$2" 2>/dev/null | sed 's/":[[:space:]]*"/":"/g' | grep -E \
      'turn_duration|stop_hook_summary|task_complete|turn_aborted|prompt\.completed|"type":"turn\.ended"|"type":"user"|"type":"assistant"|task_started|item_completed|token_count|prompt\.submitted|llm\.request|llm\.response|step\.start|step\.end|tool\.call|agent\.message\.appended' \
      2>/dev/null | tail -n 1)
    case "$1" in
      claude) case "$_last" in
                *'"subtype":"turn_duration"'*|*'"subtype":"stop_hook_summary"'*) _st=idle ;;
                *'"type":"user"'*|*'"type":"assistant"'*) _st=busy ;;
              esac ;;
      codex)  case "$_last" in
                *'"type":"task_complete"'*|*'"type":"turn_aborted"'*) _st=idle ;;
                *'"type":"task_started"'*|*'"type":"item_completed"'*|*'"type":"token_count"'*) _st=busy ;;
              esac ;;
      kimi)   case "$_last" in
                *'"type":"prompt.completed"'*|*'"type":"turn.ended"'*) _st=idle ;;
                *'"type":"prompt.submitted"'*|*'"type":"llm.request"'*|*'"type":"llm.response"'*|\
*'"type":"step.start"'*|*'"type":"step.end"'*|*'"type":"tool.call"'*|*'"type":"agent.message.appended"'*) _st=busy ;;
              esac ;;
    esac
  fi
  if [ "$_st" = busy ] && [ -n "$2" ] && [ -f "$2" ]; then
    _mt=$(stat -c %Y -- "$2" 2>/dev/null || stat -f %m -- "$2" 2>/dev/null || echo 0)
    if [ -n "$_mt" ] && [ "$_mt" -gt 0 ]; then
      _age=$(( $(date +%s) - _mt ))
      [ "$_age" -gt "${TMUXWEB_STATUS_STALE_SECONDS:-600}" ] && _st=idle
    fi
  fi
  printf '%s\n' "$_st"
}
"""
