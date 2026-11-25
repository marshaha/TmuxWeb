"""Busy/idle from the agent's own session file.

Screen scraping guesses: a status bar that says "thinking: high" looks like a running
turn, and a TUI that redraws while idle looks busy. Every agent we support does record
the truth in its session file, at the end of the turn:

    claude  {"type":"system","subtype":"turn_duration",...}   <- turn finished
    codex   {"type":"event_msg","payload":{"type":"task_complete"}}
    kimi    {"type":"prompt.completed"}

so the last complete line is enough: it is one seek plus one read, no full parse.
"""

import hashlib
import json
import os
from pathlib import Path
import time

import providers

BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"

TAIL_BYTES = 65536

# A record that ends a turn (idle) versus one that belongs to a running turn (busy).
# From the providers/ registry — the single source of truth. These names
# stay as module-level aliases so existing callers keep working.
IDLE_MARKERS = {spec.name: spec.idle_markers for spec in providers.all_specs()}
BUSY_MARKERS = {spec.name: spec.busy_markers for spec in providers.all_specs()}


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


def classify_lines(provider: str, records, turn=None) -> str:
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
            if turn is not None and verdict == IDLE and '"turn_aborted"' not in record:
                turn["end"] = hashlib.sha256(record.encode()).hexdigest()[:24]
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


def status_for_file(provider: str, path, turn=None) -> str:
    verdict = classify_lines(provider, tail_lines(path), turn)
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


# The outstanding-call predicates live in providers/ (one per harness);
# these names stay as aliases so existing callers keep working.
_outstanding_claude = providers.get("claude").outstanding
_outstanding_codex = providers.get("codex").outstanding
_outstanding_kimi = providers.get("kimi").outstanding


def has_outstanding_call(provider: str, path) -> bool:
    """Is there a tool call in the session tail with no result yet?

    The predicate is the provider's own, from the registry. Providers
    without a decision signal (hermes) report False.
    """
    provider = (provider or "").lower()
    spec = providers.get(provider)
    if spec is None or not spec.gated:
        return False
    try:
        records = _tail_records(path)
    except OSError:
        return False
    return bool(spec.outstanding(records))


def decision_point_for_file(provider: str, path) -> bool:
    """The "decision point" disjunction: the turn ended, OR a tool call is
    outstanding. The second disjunct is what a permission wait looks like in
    the file — the turn-end marker never fires there (0/4008, see above)."""
    if status_for_file(provider, path) == IDLE:
        return True
    return has_outstanding_call(provider, path)


def _session_path(provider: str, cwd: str, title: str = "", home=None, screen=None):
    """Locate a pane's session file via the provider registry, including
    the Kimi "newest updatedAt" rule. None when it cannot be found."""
    spec = providers.get(provider or "")
    if spec is None:
        return None
    try:
        return spec.resolve_session_file(home, cwd, title, screen=screen)
    except (OSError, ValueError, RuntimeError):
        return None


def status_for_pane(provider: str, cwd: str, title: str = "", home=None, screen=None, *, turn=None) -> str:
    """Locate a pane's session file and read its verdict.

    Resolution is the same code the transcript reader uses, including the Kimi
    "newest updatedAt" rule, so status and transcript can never disagree about which
    session is live.
    """
    path = _session_path(provider, cwd, title, home, screen)
    if path is None:
        return UNKNOWN
    if turn is not None:
        turn.update(session=hashlib.sha256(os.fsencode(path)).hexdigest()[:24], end="")
    return status_for_file(provider, path, turn)


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
# status_of's marker lists are BUILT from the registry at import time,
# so the shell cannot drift from the Python rules; decision_of stays a
# hand-written approximation (it only checks the NEWEST call, while the
# Python side tracks every id — fail-closed, and covered by the
# shell-agreement tests in tests/test_agent_status.py).
def _shell_case(markers):
    """One case-branch alternative per marker: *'"marker"'*|..."""
    return "|".join("*'%s'*" % marker for marker in markers)


def _shell_grep_pattern():
    """grep -E pattern over every decisive substring (dots escaped)."""
    markers = []
    for spec in providers.all_specs():
        markers.extend(spec.idle_markers)
        markers.extend(spec.busy_markers)
    return "|".join(marker.replace(".", r"\.") for marker in markers)


_STATUS_SHELL_TEMPLATE = r"""
status_of() {  # $1 = provider, $2 = session file -> busy|idle|unknown
  _st=unknown
  if [ -n "$2" ] && [ -f "$2" ]; then
    # newest decisive record: the last line that says either "turn ended" or "turn running"
    _last=$(tail -n 200 -- "$2" 2>/dev/null | sed 's/":[[:space:]]*"/":"/g' | grep -E \
      '__GREP_PATTERN__' \
      2>/dev/null | tail -n 1)
    case "$1" in
      claude) case "$_last" in
                __CLAUDE_IDLE__) _st=idle ;;
                __CLAUDE_BUSY__) _st=busy ;;
              esac ;;
      codex)  case "$_last" in
                __CODEX_IDLE__) _st=idle ;;
                __CODEX_BUSY__) _st=busy ;;
              esac ;;
      kimi)   case "$_last" in
                __KIMI_IDLE__) _st=idle ;;
                __KIMI_BUSY__) _st=busy ;;
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

STATUS_SHELL = (
    _STATUS_SHELL_TEMPLATE
    .replace("__GREP_PATTERN__", _shell_grep_pattern())
    .replace("__CLAUDE_IDLE__", _shell_case(providers.get("claude").idle_markers))
    .replace("__CLAUDE_BUSY__", _shell_case(providers.get("claude").busy_markers))
    .replace("__CODEX_IDLE__", _shell_case(providers.get("codex").idle_markers))
    .replace("__CODEX_BUSY__", _shell_case(providers.get("codex").busy_markers))
    .replace("__KIMI_IDLE__", _shell_case(providers.get("kimi").idle_markers))
    .replace("__KIMI_BUSY__", _shell_case(providers.get("kimi").busy_markers))
)
