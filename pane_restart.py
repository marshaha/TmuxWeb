"""Restart one agent pane in place and resume its session (重启并恢复).

Context lives in the session file, not in the TUI process: after the harness
binary is upgraded, the pane is respawned with the same command plus the
provider's resume flag, so the conversation continues under the new binary.

The flow is deliberately conservative: it refuses when the session cannot be
named, when the agent may still be mid-turn, and when the new process cannot
be shown to have resumed the same session. A failed verification is reported,
never papered over: a respawn into a fresh session would silently lose the
conversation this exists to preserve.
"""

from __future__ import annotations

import os
import re
import time

import agent_status

SUPPORTED_PROVIDERS = ("claude", "codex", "kimi", "hermes")
# kimi's --resume is a documented alias for --session (kimi.com docs and the
# MoonshotAI changelog, 1.40.0): `kimi --resume <id>` resumes that session.
_FLAG_PROVIDERS = ("claude", "kimi", "hermes")

SESSION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")

TERM_WAIT_SECONDS = float(os.environ.get("TMUXWEB_RESTART_TERM_SECONDS", "10"))
VERIFY_TIMEOUT_SECONDS = float(os.environ.get("TMUXWEB_RESTART_VERIFY_SECONDS", "30"))
VERIFY_POLL_SECONDS = 2.0
HERMES_QUIET_SECONDS = float(os.environ.get("TMUXWEB_RESTART_HERMES_QUIET_SECONDS", "30"))


class RestartResumeError(RuntimeError):
    """A 409-class refusal: unsafe to respawn, or the resume did not verify."""


def _valid_session_id(session_id) -> bool:
    return isinstance(session_id, str) and SESSION_ID_RE.match(session_id) is not None


def _agent_token_index(argv, provider):
    """Index of the token that names the agent binary (or its script)."""
    for index, token in enumerate(argv):
        base = os.path.basename(str(token)).lower()
        if base == provider or base.startswith(provider + ".") or base.startswith(provider + "-"):
            return index
    return 0


def resume_argv(provider, argv, session_id):
    """``argv`` with the provider's resume flag added (or its id replaced).

    Raises ValueError for an unsupported provider, an empty argv, or a
    session id that does not look resumable. The input list is not mutated.
    """
    provider = (provider or "").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("restart-and-resume is not supported for provider %r"
                         % (provider or ""))
    if not isinstance(argv, (list, tuple)) or not argv \
            or not all(isinstance(token, str) for token in argv):
        raise ValueError("a non-empty argv list is required")
    if not _valid_session_id(session_id):
        raise ValueError("invalid session id")
    argv = list(argv)
    if provider == "kimi":
        argv = _strip_kimi_resume_conflicts(argv)
    if provider == "codex":
        return _resume_codex(argv, session_id)
    return _resume_flag(argv, session_id, provider)


# Kimi flags that cannot appear next to a resume flag: --agent/--agent-file
# and --environment/--environment-cwd only apply when a session is created,
# and --continue means "resume the latest" — all of them are rejected at
# startup next to --session/--resume (kimi.com/docs kimi-command: Flag
# Conflict Rules). A respawn that kept them would fail outright, so drop
# them (with their values) before adding --resume. --continue is the
# documented fallback if an installed kimi predates the --resume alias: it
# resumes the latest session in the cwd, so it is NOT wired in as an
# automatic fallback here — with no session id there is no idle signal and
# no proof the latest session is this pane's.
_KIMI_RESUME_CONFLICTS = ("--agent", "--agent-file", "--environment", "--environment-cwd")


def _strip_kimi_resume_conflicts(argv):
    kept = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--continue", "-c"):
            index += 1
            continue
        if token in _KIMI_RESUME_CONFLICTS:
            index += 2  # the flag takes a value
            continue
        if any(token.startswith(flag + "=") for flag in _KIMI_RESUME_CONFLICTS):
            index += 1
            continue
        kept.append(token)
        index += 1
    return kept


def _resume_flag(argv, session_id, provider):
    """claude / kimi / hermes: ``--resume <id>`` (or an equivalent flag)."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--resume":
            if index + 1 < len(argv):
                argv[index + 1] = session_id
            else:
                argv.append(session_id)
            return argv
        if token.startswith("--resume="):
            argv[index] = "--resume=" + session_id
            return argv
        if token == "-r":
            if index + 1 < len(argv):
                argv[index + 1] = session_id
            else:
                argv.append(session_id)
            return argv
        if token == "--session-id":
            # starts a *new* session with that id; resuming needs --resume
            argv[index] = "--resume"
            if index + 1 < len(argv):
                argv[index + 1] = session_id
            else:
                argv.append(session_id)
            return argv
        if token.startswith("--session-id="):
            argv[index] = "--resume=" + session_id
            return argv
        if provider == "kimi" and token == "--session":
            if index + 1 < len(argv):
                argv[index + 1] = session_id
            else:
                argv.append(session_id)
            return argv
        if provider == "kimi" and token == "-S":
            if index + 1 < len(argv):
                argv[index + 1] = session_id
            else:
                argv.append(session_id)
            return argv
        if provider == "kimi" and token.startswith("--session="):
            argv[index] = "--session=" + session_id
            return argv
        index += 1
    argv.extend(["--resume", session_id])
    return argv


def _resume_codex(argv, session_id):
    """codex: ``resume <id>`` is a subcommand, not a flag."""
    for index, token in enumerate(argv):
        if token == "resume":
            if index + 1 < len(argv) and SESSION_ID_RE.match(argv[index + 1]):
                argv[index + 1] = session_id
            else:
                argv.insert(index + 1, session_id)
            return argv
    at = _agent_token_index(argv, "codex")
    argv[at + 1:at + 1] = ["resume", session_id]
    return argv


def hermes_idle(data, quiet_seconds=HERMES_QUIET_SECONDS):
    """Hermes writes no turn-boundary record, so idleness is a quiet window:
    the last message is an assistant one and the state.db has not been
    written to for ``quiet_seconds``. A streaming turn writes constantly, so
    a quiet file is the only signal there is; it is a heuristic, not a
    proof, and the caller says so."""
    messages = (data or {}).get("messages") or []
    if not messages or (messages[-1].get("role") or "") != "assistant":
        return False
    mtime = (data or {}).get("mtime") or 0
    try:
        mtime = float(mtime)
    except (TypeError, ValueError):
        return False
    if mtime > 1e12:  # the local reader reports nanoseconds, the remote one seconds
        mtime /= 1e9
    if mtime <= 0:
        return False
    return (time.time() - mtime) >= quiet_seconds


def verify_resumed(get_info, get_session_id, session_id, old_pid,
                   timeout=VERIFY_TIMEOUT_SECONDS, poll=VERIFY_POLL_SECONDS,
                   sleep=time.sleep):
    """Poll until the new process is provably the resumed one.

    ``get_info`` returns {"pane_pid", "session_id"} for the live process tree;
    ``get_session_id`` returns the transcript's session id, or None when the
    transcript is not readable yet (a session with no messages has nothing to
    read). Verified means the pane's root process changed, the live argv
    carries --resume <session_id>, and the transcript agrees when it can be
    read at all. Returns (verified, detail).
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    last_detail = "no verification attempt completed"
    while True:
        try:
            info = get_info() or {}
        except Exception:  # noqa: BLE001 - a failed probe is "not yet", not fatal
            info = {}
        new_pid = str(info.get("pane_pid") or "")
        hint = str(info.get("session_id") or "")
        pid_ok = bool(old_pid) and bool(new_pid) and new_pid != str(old_pid)
        hint_ok = bool(hint) and hint == session_id
        if pid_ok and hint_ok:
            try:
                transcript_id = get_session_id()
            except Exception:  # noqa: BLE001
                transcript_id = None
            transcript_id = str(transcript_id) if transcript_id else None
            if transcript_id is None:
                return True, "verified by pid + resume flag (transcript not yet readable)"
            if transcript_id == session_id:
                return True, "verified by pid + resume flag + transcript"
            return False, ("the transcript now shows session %s, not %s"
                           % (transcript_id, session_id))
        if not pid_ok:
            last_detail = ("could not read the new pane process"
                           if not new_pid else "the pane process has not been replaced yet")
        elif not hint_ok:
            last_detail = ("the new process shows no resume flag yet"
                           if not hint else
                           "the new process resumed session %s, not %s" % (hint, session_id))
        if time.monotonic() >= deadline:
            return False, last_detail
        sleep(max(0.0, float(poll)))


def default_idle_check(transport, pane_id, pane, provider, data):
    """'idle' | 'busy' | 'unknown'. Local panes read their own session file
    (the transcript's own file when it is known, so the verdict is about the
    session being resumed, not a sibling in the same directory); remote panes
    ask the host (status_many); Hermes has no turn-boundary record, so it
    uses the quiet-window heuristic on the transcript."""
    if provider == "hermes":
        return "idle" if hermes_idle(data) else "busy"
    if (pane or {}).get("host_id") in (None, "localhost"):
        path = (data or {}).get("path") or ""
        try:
            if path:
                return agent_status.status_for_file(provider, path)
            return agent_status.status_for_pane(
                provider, (pane or {}).get("path", ""), (pane or {}).get("title", ""))
        except Exception:  # noqa: BLE001 - a failed probe is "unknown", not idle
            return "unknown"
    try:
        states = transport.status_many([pane_id])
    except Exception:  # noqa: BLE001
        return "unknown"
    return str(states.get(pane_id) or "unknown")


def restart_and_resume(transport, pane_id, pane, provider, *, idle_check=None,
                       term_wait=TERM_WAIT_SECONDS,
                       verify_timeout=VERIFY_TIMEOUT_SECONDS,
                       verify_poll=VERIFY_POLL_SECONDS, sleep=time.sleep):
    """The full flow. Raises RestartResumeError on any refusal or failed
    verification; transport errors (TmuxError/RuntimeError/OSError) pass
    through for the endpoint to report as 503."""
    provider = (provider or "").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("restart-and-resume is not supported for provider %r" % provider)
    check = idle_check or default_idle_check
    info = transport.restart_info(pane_id, pane=pane)
    old_pid = str((info or {}).get("pane_pid") or "")
    hint = str((info or {}).get("session_id") or "")
    argv = list((info or {}).get("argv") or [])
    if not old_pid:
        raise RestartResumeError(
            "could not read the pane process; refusing to respawn blind")
    if not argv:
        raise RestartResumeError(
            "could not read the agent command for this pane; refusing to respawn blind")
    data = None
    transcript_id = ""
    try:
        data = transport.transcript(pane_id, 1, pane=pane)
        transcript_id = str((data or {}).get("session_id") or "")
    except Exception:  # noqa: BLE001 - the argv hint below may still name the session
        data = None
    session_id = hint or transcript_id
    if not session_id:
        raise RestartResumeError(
            "no session could be named for this pane, so there is nothing safe to resume into")
    if not _valid_session_id(session_id):
        raise RestartResumeError(
            "the session id %r does not look resumable" % session_id)
    state = check(transport, pane_id, pane, provider, data)
    if state != "idle":
        raise RestartResumeError(
            "the agent is %s; restart-and-resume only runs while it is idle" % state)
    command = resume_argv(provider, argv, session_id)
    transport.restart_resume(pane_id, command, pane=pane, term_wait=term_wait)

    def _info():
        return transport.restart_info(pane_id, pane=pane)

    def _session():
        try:
            current = transport.transcript(pane_id, 1, refresh=True, pane=pane)
        except Exception:  # noqa: BLE001 - unreadable is "not yet", handled by verify
            return None
        return (current or {}).get("session_id") or None

    ok, detail = verify_resumed(_info, _session, session_id, old_pid,
                                timeout=verify_timeout, poll=verify_poll, sleep=sleep)
    if not ok:
        raise RestartResumeError(
            "restarted, but the resume did not verify: %s" % detail)
    return {"status": "resumed", "pane": pane_id, "provider": provider,
            "session_id": session_id, "command": command, "verified": True,
            "detail": detail}
