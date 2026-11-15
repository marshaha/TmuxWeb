#!/usr/bin/env python3
"""TmuxWeb "needs you" hook receiver (one shared script, all providers).

A hook fires, this script appends one JSON line to the agent's event
file (``~/.config/tmuxweb-agent/events.jsonl``, next to config.json),
and exits 0. The agent program tails that file and holds per-pane
waiting state; the server merges it with its own gate (PR #24).

Usage (the hook command carries the event name as argv[1] — the
configured event is authoritative, so the script never has to guess
it from a payload field whose name may differ per provider/version):

    tmuxweb-hook.py permission_prompt      # Claude Notification matcher
    tmuxweb-hook.py Stop                   # Claude / Codex / Kimi Stop
    tmuxweb-hook.py PermissionRequest      # Codex / Kimi
    tmuxweb-hook.py PermissionResult       # Kimi

The event payload arrives on stdin as JSON. Field names are read
tolerantly (see ``parse_hook_event`` in agent.py): ``session_id`` /
``sessionId`` / ``thread_id``, ``cwd``, ``hook_event_name`` /
``event`` / ``notification_type`` — and a missing session id falls
back to the stem of ``transcript_path`` or to a cwd match.

Fail-open, always: any error (bad stdin, missing directory, full
disk) is swallowed and the exit code is still 0, so a hook failure
can never block the agent it observes. The script never dials out:
it writes to a local file only. The event file is capped at 256KB
(the agent rotates it); this script also keeps only the newest 1000
lines if it finds the file over the cap.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MAX_EVENTS_BYTES = 256 * 1024
MAX_EVENTS_KEPT = 1000


def events_path() -> Path:
    override = os.environ.get("TMUXWEB_AGENT_EVENTS")
    if override:
        return Path(override)
    return Path.home() / ".config" / "tmuxweb-agent" / "events.jsonl"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    configured_event = argv[1] if len(argv) > 1 else ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    if configured_event:
        # The configured event wins: the hook that fired is known by
        # its config entry, not by a payload field we may have guessed.
        payload = dict(payload)
        payload["hook_event_name"] = configured_event
    event = {
        "session_id": payload.get("session_id") or payload.get("sessionId")
        or payload.get("thread_id") or "",
        "cwd": payload.get("cwd") or "",
        "pane_id": payload.get("pane_id") or "",
        "transcript_path": payload.get("transcript_path") or "",
        "hook_event_name": payload.get("hook_event_name")
        or payload.get("event") or payload.get("notification_type") or "",
        "timestamp": time.time(),
    }
    if not event["hook_event_name"]:
        return 0  # nothing this watcher understands; still exit 0
    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            if path.stat().st_size > MAX_EVENTS_BYTES:
                lines = path.read_text(encoding="utf-8",
                                       errors="replace").splitlines()
                kept = lines[-MAX_EVENTS_KEPT:]
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                               encoding="utf-8")
                os.replace(tmp, path)
        except OSError:
            pass
    except Exception:
        pass  # fail-open: a hook must never block the agent
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
