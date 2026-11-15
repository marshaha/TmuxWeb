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


# An "outstanding" tool call: the agent issued it and no result has come back.
# This is the state a permission prompt lives in. Real session files prove the
# turn-end marker does NOT cover it: across two Claude files (10432 + 45645
# records) 0 of 4008 tool_use blocks are followed by a turn_duration before the
# next user message carrying the tool_result — turn_duration is written only
# after the tool result returns and the turn completes. So while the agent sits
# at a permission prompt, the file's last decisive record is the assistant
# message and the verdict is BUSY. The outstanding call is the signal instead.
def _tail_records(path):
    """Parsed JSON records from the tail, oldest first; unparsable lines skipped."""
    records = []
    for line in tail_lines(path):
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _outstanding_claude(records) -> bool:
    """A tool_use id with no tool_result yet. A plain user message after the
    call means the user intervened and the call is abandoned; a turn-end
    marker closes everything before it."""
    pending: set = set()
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


CODEX_CALL_TYPES = ("function_call", "custom_tool_call")
CODEX_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")


def _outstanding_codex(records) -> bool:
    """A call item with no matching output item. Codex writes NO explicit
    approval-wait event: the permission/approval lines in a real rollout file
    are approval_policy settings, thread_settings_applied, and content mentions
    inside outputs — settings and quotes, not a wait event."""
    pending: set = set()
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
        if payload.get("type") in CODEX_CALL_TYPES:
            pending.add(key)
        elif payload.get("type") in CODEX_OUTPUT_TYPES:
            pending.discard(key)
    return bool(pending)


# Kimi has no turn marker for a wait either (state.json lastTurnReason is only
# ever 'completed'): the event stream is the signal. A tool.call opens a
# pending call; the step/turn ending, a new prompt, or the next llm.request
# (which only fires once the tool result is back) closes it.
KIMI_CALL_CLOSERS = ("prompt.completed", "turn.ended", "step.end",
                     "prompt.submitted", "llm.request")


def _outstanding_kimi(records) -> bool:
    pending = False
    for event in records:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "tool.call":
            pending = True
        elif kind in KIMI_CALL_CLOSERS:
            pending = False
        elif kind == "context.append_loop_event":
            if (event.get("event") or {}).get("type") == "step.end":
                pending = False
    return pending


def has_outstanding_call(provider: str, path) -> bool:
    """Is there a tool call in the session tail with no result yet?"""
    provider = (provider or "").lower()
    if provider not in ("claude", "codex", "kimi"):
        return False
    try:
        records = _tail_records(path)
    except OSError:
        return False
    if provider == "claude":
        return _outstanding_claude(records)
    if provider == "codex":
        return _outstanding_codex(records)
    return _outstanding_kimi(records)


def decision_point_for_file(provider: str, path) -> bool:
    """The "decision point" disjunction: the turn ended, OR a tool call is
    outstanding. The second disjunct is what a permission wait looks like in
    the file — the turn-end marker never fires there (0/4008, see above)."""
    if status_for_file(provider, path) == IDLE:
        return True
    return has_outstanding_call(provider, path)


def _session_path(provider: str, cwd: str, title: str = "", home=None, screen=None):
    """Locate a pane's session file the way the transcript reader does,
    including the Kimi "newest updatedAt" rule. None when it cannot be found."""
    import transcript_reader

    home = home or os.path.expanduser("~")
    try:
        if provider == "claude":
            try:
                return transcript_reader._claude_session_path(home, cwd, title, screen, None)
            except transcript_reader.TranscriptError:
                # several sessions, none attributable: the newest one is the live one
                import re
                project = (Path(home) / ".claude" / "projects"
                           / re.sub(r"[^A-Za-z0-9]", "-", cwd or ""))
                candidates = sorted(project.glob("*.jsonl"), key=lambda item: item.stat().st_mtime,
                                    reverse=True) if project.is_dir() else []
                return candidates[0] if candidates else None
        if provider == "codex":
            _, path = transcript_reader._codex_session_path(home, cwd, title)
            return path
        if provider == "kimi":
            _, path = transcript_reader._kimi_session_path(home, cwd, title)
            return path
    except (OSError, ValueError, RuntimeError, transcript_reader.TranscriptError):
        return None
    return None


def status_for_pane(provider: str, cwd: str, title: str = "", home=None, screen=None) -> str:
    """Locate a pane's session file and read its verdict.

    Resolution is the same code the transcript reader uses, including the Kimi
    "newest updatedAt" rule, so status and transcript can never disagree about which
    session is live.
    """
    path = _session_path(provider, cwd, title, home, screen)
    if path is None:
        return UNKNOWN
    return status_for_file(provider, path)


def decision_point_for_pane(provider: str, cwd: str, title: str = "", home=None,
                            screen=None) -> bool:
    """Is this pane at a decision point (turn ended, or a tool call outstanding)?

    Same session-file resolution as status_for_pane, so the two can never
    disagree about which session is live. False when the file cannot be found
    or the provider has no such signal — fail closed.
    """
    path = _session_path(provider, cwd, title, home, screen)
    if path is None:
        return False
    return decision_point_for_file(provider, path)


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

decision_of() {  # $1 = provider, $2 = session file -> yes|no
  # yes if the turn ended, or a tool call is outstanding (a permission wait
  # never writes a turn-end marker, so "idle" alone misses exactly the case
  # that matters — see decision_point_for_file in agent_status.py).
  # The shell only checks the NEWEST call: an older parallel call still
  # pending while a newer one finished is missed — fail-closed, and the
  # Python side tracks every id exactly.
  _st=$(status_of "$1" "$2")
  [ "$_st" = idle ] && { printf 'yes\n'; return; }
  [ -n "$2" ] && [ -f "$2" ] || { printf 'no\n'; return; }
  _tail=$(tail -n 200 -- "$2" 2>/dev/null | sed 's/":[[:space:]]*"/":"/g')
  [ -n "$_tail" ] || { printf 'no\n'; return; }
  case "$1" in
    claude)
      _use_line=$(printf '%s\n' "$_tail" | grep -n '"type":"tool_use"' | tail -n 1 | cut -d: -f1)
      [ -n "$_use_line" ] || { printf 'no\n'; return; }
      _use=$(printf '%s\n' "$_tail" | sed -n "${_use_line}p" | grep -o '"id":"[^"]*"' | head -n 1 | sed 's/.*"id":"//;s/"$//')
      [ -n "$_use" ] || { printf 'no\n'; return; }
      _after=$(printf '%s\n' "$_tail" | awk -v n="$_use_line" 'NR>n')
      case "$_after" in
        *'"tool_use_id":"'"$_use"'"'*) printf 'no\n' ;;   # the result came back
        *'"type":"user"'*) printf 'no\n' ;;               # the user intervened
        *'"subtype":"turn_duration"'*|*'"subtype":"stop_hook_summary"'*) printf 'no\n' ;;
        *) printf 'yes\n' ;;
      esac ;;
    codex)
      _call_line=$(printf '%s\n' "$_tail" | grep -nE '"type":"(function_call|custom_tool_call)"' | tail -n 1 | cut -d: -f1)
      [ -n "$_call_line" ] || { printf 'no\n'; return; }
      _call=$(printf '%s\n' "$_tail" | sed -n "${_call_line}p" | grep -o '"call_id":"[^"]*"' | head -n 1 | sed 's/.*"call_id":"//;s/"$//')
      [ -n "$_call" ] || { printf 'no\n'; return; }
      _after=$(printf '%s\n' "$_tail" | awk -v n="$_call_line" 'NR>n')
      case "$_after" in
        *'"call_id":"'"$_call"'"'*) printf 'no\n' ;;      # the output came back
        *'"type":"task_complete"'*|*'"type":"turn_aborted"'*|*'"type":"task_started"'*) printf 'no\n' ;;
        *) printf 'yes\n' ;;
      esac ;;
    kimi)
      _call_line=$(printf '%s\n' "$_tail" | grep -n '"type":"tool.call"' | tail -n 1 | cut -d: -f1)
      [ -n "$_call_line" ] || { printf 'no\n'; return; }
      _after=$(printf '%s\n' "$_tail" | awk -v n="$_call_line" 'NR>n')
      case "$_after" in
        *'"type":"prompt.completed"'*|*'"type":"turn.ended"'*|*'"type":"step.end"'*|\
*'"type":"prompt.submitted"'*|*'"type":"llm.request"'*) printf 'no\n' ;;
        *) printf 'yes\n' ;;
      esac ;;
    *) printf 'no\n' ;;
  esac
}
"""
