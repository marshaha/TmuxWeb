from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import tempfile
import shlex
import logging
import fcntl

import file_browser
import struct
import termios
import threading
import asyncio
from pathlib import Path

from cryptography.fernet import Fernet

import transcript_reader

HOST_ID_RE = re.compile(r"^[a-f0-9]{16}$")
AUTH_TYPES = ("password", "key", "agent")
# tmux splits targets on ":" and ".". Keep new session names boring so the
# name can be handed to tmux and to our pane references unchanged.
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def normalize_session_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not SESSION_NAME_RE.fullmatch(cleaned):
        raise ValueError("session name must be 1-64 characters: letters, digits, dot, dash or underscore")
    if ":" in cleaned:
        raise ValueError("session name must not contain ':'")
    return cleaned

PANE_ID_RE = re.compile(r"^%[0-9]+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
# tmux escapes control characters in ``-F`` output when the client locale is
# not UTF-8 (a TAB becomes "_"), and rewrites non-ASCII text the same way. A
# printable separator plus a forced UTF-8 locale keep remote fields intact.
FIELD_SEPARATOR = "<::>"
LOGGER = logging.getLogger("tmuxweb.remote")

LOCALE_PRELUDE = "export LC_ALL=${TMUXWEB_REMOTE_LC:-C.UTF-8} LANG=${TMUXWEB_REMOTE_LC:-C.UTF-8}; "

# macOS keeps tmux in /opt/homebrew/bin, which a non-interactive ssh command does
# not have on PATH: resolve the binary once per host and hand it to the scripts.
TMUX_PROBE_SCRIPT = r"""
tmux_path=$(command -v tmux 2>/dev/null)
if [ -z "$tmux_path" ]; then
  for candidate in /opt/homebrew/bin/tmux /usr/local/bin/tmux /opt/local/bin/tmux "$HOME/bin/tmux" "$HOME/.local/bin/tmux"; do
    if [ -x "$candidate" ]; then tmux_path=$candidate; break; fi
  done
fi
if [ -z "$tmux_path" ]; then
  # A login shell can hang for ages on a slow host (rc files doing network work),
  # and this probe runs before every other command: bound it when `timeout` exists.
  bound=""
  if command -v timeout >/dev/null 2>&1; then bound="timeout 4"; fi
  for shell_name in bash zsh; do
    if command -v "$shell_name" >/dev/null 2>&1; then
      candidate=$($bound "$shell_name" -lc 'command -v tmux' 2>/dev/null | tr -d '\r' | head -1)
      if [ -n "$candidate" ]; then tmux_path=$candidate; break; fi
    fi
  done
fi
printf '%s' "$tmux_path"
"""

TMUX_MISSING_HINT = (
    "tmux is not installed (or not on PATH) on this host; checked PATH, /opt/homebrew/bin, "
    "/usr/local/bin. macOS: brew install tmux. Linux: sudo apt install tmux."
)

_TMUX_BIN_CACHE: dict[str, str] = {}

# How many tail bytes a transcript pull asks for. Starts generous and adapts per
# host: a slow pull halves it, a quick one doubles it back.
PAYLOAD_WINDOW_DEFAULT = 4 * 1024 * 1024
PAYLOAD_WINDOW_MIN = 1024 * 1024   # below this the tail holds too few messages to be useful
PAYLOAD_WINDOW_MAX = 4 * 1024 * 1024
_PAYLOAD_WINDOW: dict[str, int] = {}


def payload_window(host: dict) -> int:
    return _PAYLOAD_WINDOW.get(str(host.get("id") or host.get("hostname") or ""), PAYLOAD_WINDOW_DEFAULT)


def note_pull_duration(host: dict, elapsed: float) -> None:
    """Shrink the window on a slow host, grow it back on a fast one."""
    key = str(host.get("id") or host.get("hostname") or "")
    current = _PAYLOAD_WINDOW.get(key, PAYLOAD_WINDOW_DEFAULT)
    if elapsed > 8.0:
        wanted = max(PAYLOAD_WINDOW_MIN, current // 2)
    elif elapsed < 2.5:
        wanted = min(PAYLOAD_WINDOW_MAX, current * 2)
    else:
        return
    if wanted != current:
        _PAYLOAD_WINDOW[key] = wanted
        log = LOGGER.warning if wanted < current else LOGGER.info
        log("transcript window for %s: %d KB -> %d KB (pull took %.1fs)",
            host.get("name") or key, current // 1024, wanted // 1024, elapsed)
# A failed probe costs a full SSH round trip; remember it briefly so a bad link does
# not pay for it on every request, but do retry later in case the host recovers.
_TMUX_BIN_FAILED: dict[str, float] = {}
_TMUX_BIN_RETRY_AFTER = 60.0


def tmux_cache_key(host: dict) -> str:
    return "%s|%s|%s" % (host.get("id", ""), host.get("hostname", ""), host.get("port", ""))
# Remote panes are read the same way ``scp`` would: a POSIX shell snippet finds
# the provider's transcript file (or exports rows with sqlite3 for Hermes) and
# the bytes are pulled here and parsed by transcript_reader. No remote python.
REMOTE_TRANSCRIPT_SCRIPT = r"""
pane=$1
limit=${2:-200}
sep='<::>'
header() {
  printf 'TMUXWEB_PROVIDER%s%s\nTMUXWEB_SESSION%s%s\nTMUXWEB_FORMAT%s%s\nTMUXWEB_ENCODING%s%s\nTMUXWEB_TITLE%s%s\n' \
    "$sep" "$provider" "$sep" "$1" "$sep" "$2" "$sep" "$encoding" "$sep" "$title"
  if [ -n "$3" ] && [ -e "$3" ]; then
    printf 'TMUXWEB_PRIMARY%s%s\n' "$sep" "$3"
    stamps=$(stat -c '%Y %s' -- "$3" 2>/dev/null || stat -f '%m %z' -- "$3" 2>/dev/null)
    printf 'TMUXWEB_STAMP%s%s\n' "$sep" "$stamps"
  fi
  printf 'TMUXWEB_PAYLOAD\n'
}
error() { printf 'TMUXWEB_ERROR%s%s\n' "$sep" "$1"; exit "${2:-20}"; }
payload_bytes=${TMUXWEB_PAYLOAD_BYTES:-4194304}
encoding=none
if command -v gzip >/dev/null 2>&1; then encoding=gzip; fi
emit() { if [ "$encoding" = gzip ]; then gzip -c; else cat; fi; }

info=$(${TMUX_BIN:-tmux} display-message -p -t "$pane" \
  "#{pane_current_path}${sep}#{pane_title}${sep}#{pane_current_command}" 2>/dev/null) \
  || error "pane not found: $pane" 21
cwd=${info%%"$sep"*}
rest=${info#*"$sep"}
title=${rest%%"$sep"*}
command=${rest##*"$sep"}
case "$command" in
  claude*) provider=claude ;;
  codex*) provider=codex ;;
  kimi*) provider=kimi ;;
  python*|hermes*) provider=hermes ;;
  *) provider="" ;;
esac
[ -n "$provider" ] || error "unsupported pane command: $command" 22

if [ "$provider" = claude ]; then
  proj=$HOME/.claude/projects/$(printf '%s' "$cwd" | sed 's/[^A-Za-z0-9]/-/g')
  [ -d "$proj" ] || error "no Claude project directory for $cwd" 23
  file=""
  # Only the newest few files can belong to a live pane, and only their tail is
  # scanned: grepping whole 300 MB transcripts once per probe line took 50s+.
  candidates=$(ls -t "$proj"/*.jsonl 2>/dev/null | head -3)
  if [ -n "$title" ]; then
    for candidate in $candidates; do
      if tail -c 262144 -- "$candidate" 2>/dev/null | grep -qF -- "$title"; then
        file=$candidate
        break
      fi
    done
  fi
  if [ -z "$file" ]; then
    probes=$(${TMUX_BIN:-tmux} capture-pane -p -J -t "$pane" -S -80 2>/dev/null | awk 'length($0) >= 24' | tail -20)
    while IFS= read -r probe; do
      [ -n "$probe" ] || continue
      for candidate in $candidates; do
        if tail -c 262144 -- "$candidate" 2>/dev/null | grep -qF -e "$probe"; then
          file=$candidate
          break 2
        fi
      done
    done <<PROBES
$probes
PROBES
  fi
  [ -n "$file" ] || file=$(printf '%s\n' "$candidates" | head -1)
  [ -n "$file" ] || error "no Claude transcript for $cwd" 23
  header "$(basename "$file" .jsonl)" jsonl "$file"
  tail -c "$payload_bytes" -- "$file" 2>/dev/null | emit
  exit 0
fi

if [ "$provider" = codex ]; then
  id=""
  file=""
  short=$(printf '%s' "$title" | sed 's/ |.*//')
  if [ -n "$short" ] && [ -f "$HOME/.codex/session_index.jsonl" ]; then
    id=$(grep -F "\"thread_name\": \"$short\"" "$HOME/.codex/session_index.jsonl" 2>/dev/null \
      | tail -1 | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  fi
  [ -n "$id" ] && file=$(find "$HOME/.codex/sessions" -name "*$id*.jsonl" 2>/dev/null | head -1)
  if [ -z "$file" ]; then
    candidates=$(find "$HOME/.codex/sessions" -name '*.jsonl' -mtime -30 2>/dev/null | head -300)
    for candidate in $candidates; do
      if head -c 2000 -- "$candidate" 2>/dev/null | grep -qF -- "\"cwd\": \"$cwd\"" \
        || head -c 2000 -- "$candidate" 2>/dev/null | grep -qF -- "\"cwd\":\"$cwd\""; then
        file=$candidate
        break
      fi
    done
  fi
  [ -n "$file" ] || error "no Codex transcript for $cwd" 23
  [ -n "$id" ] || id=$(basename "$file" .jsonl | sed 's/^rollout-[0-9T:.-]*-//')
  header "$id" jsonl "$file"
  tail -c "$payload_bytes" -- "$file" 2>/dev/null | emit
  exit 0
fi

if [ "$provider" = kimi ]; then
  # Several sessions can share a cwd and a title, and only the one being written now
  # has messages: pick the newest by updatedAt (falling back to the file mtime).
  # Picking the first glob match landed on dead sessions and reported "no messages".
  kimi_stamp() {
    value=$(sed -n 's/.*"updatedAt"[[:space:]]*:[[:space:]]*\([0-9]\{10,\}\).*/\1/p' "$1" 2>/dev/null | head -1)
    if [ -z "$value" ]; then
      value=$(stat -c %Y -- "$1" 2>/dev/null || stat -f %m -- "$1" 2>/dev/null || echo 0)
    fi
    case $value in
      [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*) value=$((value / 1000)) ;;
    esac
    printf '%s' "${value:-0}"
  }
  pick_kimi() {  # $1 = 1 -> require the pane title to appear in state.json
    best=0
    best_wire=""
    best_id=""
    for state in "$HOME"/.kimi-code/sessions/wd_*/session_*/state.json; do
      [ -f "$state" ] || continue
      grep -qF -- "$cwd" "$state" 2>/dev/null || continue
      grep -qF '"archived": true' "$state" 2>/dev/null && continue
      wire=$(dirname "$state")/agents/main/wire.jsonl
      [ -f "$wire" ] || continue
      if [ "$1" = 1 ]; then
        [ -n "$title" ] || continue
        grep -qF -- "$title" "$state" 2>/dev/null || continue
      fi
      stamp=$(kimi_stamp "$state")
      if [ "$best_wire" = "" ] || [ "$stamp" -gt "$best" ]; then
        best=$stamp
        best_wire=$wire
        best_id=$(basename "$(dirname "$state")")
      fi
    done
    found=$best_wire
    found_id=$best_id
  }
  file=""
  id=""
  found=""
  found_id=""
  pick_kimi 1
  [ -n "$found" ] || pick_kimi 0
  file=$found
  id=$found_id
  [ -n "$file" ] || error "no Kimi transcript for $cwd" 23
  header "$id" jsonl "$file"
  tail -c "$payload_bytes" -- "$file" 2>/dev/null | emit
  exit 0
fi

# hermes: prefer the per-session jsonl the CLI keeps, else query state.db
pane_pid=$(${TMUX_BIN:-tmux} display-message -p -t "$pane" '#{pane_pid}' 2>/dev/null)
sid=""
for candidate in $pane_pid $(pgrep -P "$pane_pid" 2>/dev/null) \
    $(for child in $(pgrep -P "$pane_pid" 2>/dev/null); do pgrep -P "$child" 2>/dev/null; done); do
  sid=$(tr '\0' ' ' < "/proc/$candidate/cmdline" 2>/dev/null \
    | sed -n 's/.*--resume[ =]\([0-9A-Za-z][0-9A-Za-z_-]*\).*/\1/p')
  [ -n "$sid" ] && break
done
if [ -n "$sid" ] && [ -f "$HOME/.hermes/sessions/$sid.jsonl" ]; then
  header "$sid" hermes-jsonl "$HOME/.hermes/sessions/$sid.jsonl"
  tail -c "$payload_bytes" -- "$HOME/.hermes/sessions/$sid.jsonl" 2>/dev/null | emit
  exit 0
fi
sqlite_bin=""
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite_bin=$(command -v sqlite3)
else
  # non-interactive ssh sessions often miss conda/pyenv/homebrew PATHs
  for candidate in "$HOME/miniconda3/bin/sqlite3" "$HOME/anaconda3/bin/sqlite3" \
      "$HOME/.local/bin/sqlite3" /usr/local/bin/sqlite3 /usr/bin/sqlite3 /opt/homebrew/bin/sqlite3; do
    if [ -x "$candidate" ]; then sqlite_bin=$candidate; break; fi
  done
  if [ -z "$sqlite_bin" ] && command -v bash >/dev/null 2>&1; then
    sqlite_bin=$(bash -lc 'command -v sqlite3' 2>/dev/null | tail -1)
  fi
fi
if [ -n "$sqlite_bin" ] && [ -f "$HOME/.hermes/state.db" ] \
    && "$sqlite_bin" -readonly -json :memory: 'select 1' >/dev/null 2>&1; then
  cwd_sql=$(printf '%s' "$cwd" | sed "s/'/''/g")
  if [ -z "$sid" ]; then
    sid=$("$sqlite_bin" -readonly "$HOME/.hermes/state.db" \
      "select s.id from sessions s where s.cwd='$cwd_sql' order by (select count(*) from messages m where m.session_id=s.id and m.active=1 and coalesce(m.content,'')<>'')=0, coalesce(s.last_activity_at,s.started_at) desc limit 1" 2>/dev/null)
  fi
  [ -n "$sid" ] || error "no Hermes session for $cwd" 23
  header "$sid" hermes-rows "$HOME/.hermes/state.db"
  "$sqlite_bin" -readonly -json "$HOME/.hermes/state.db" \
    "select id, role, content, timestamp from messages where session_id='$sid' and active=1 and role in ('user','assistant') and coalesce(content,'')<>'' order by id desc limit $limit" 2>/dev/null
  exit 0
fi

# last resort: a Hermes install always ships a python3 runtime, so use its
# stdlib sqlite3 module instead of requiring a sqlite3 binary.
py=""
if command -v python3 >/dev/null 2>&1; then
  py=$(command -v python3)
else
  for candidate in "$HOME"/.hermes/tools/python-*/bin/python3 "$HOME/miniconda3/bin/python3" \
      "$HOME/anaconda3/bin/python3" /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ]; then py=$candidate; break; fi
  done
fi
if [ -n "$py" ] && [ -f "$HOME/.hermes/state.db" ]; then
  exec "$py" - "$HOME/.hermes/state.db" "$cwd" "$sid" "$limit" <<'PYTHON'
import json
import os
import sqlite3
import sys

db, cwd, sid, limit = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sep = "<::>"
try:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    if not sid:
        row = con.execute(
            "select s.id from sessions s where s.cwd=? order by"
            " (select count(*) from messages m where m.session_id=s.id and m.active=1"
            "  and coalesce(m.content,'')<>'')=0,"
            " coalesce(s.last_activity_at, s.started_at) desc limit 1",
            (cwd,),
        ).fetchone()
        sid = row["id"] if row else ""
    if not sid:
        sys.exit(3)
    rows = [dict(row) for row in con.execute(
        "select id, role, content, timestamp from messages where session_id=?"
        " and active=1 and role in ('user','assistant') and coalesce(content,'')<>''"
        " order by id desc limit ?",
        (sid, limit),
    )]
except Exception as exc:  # noqa: BLE001 - report and let the caller fall back
    sys.stderr.write("hermes transcript export failed: %s\n" % exc)
    sys.exit(4)
info = os.stat(db)
sys.stdout.write(
    "TMUXWEB_PROVIDER%shermes\nTMUXWEB_SESSION%s%s\nTMUXWEB_FORMAT%shermes-rows\n"
    "TMUXWEB_PRIMARY%s%s\nTMUXWEB_STAMP%s%d %d\nTMUXWEB_PAYLOAD\n"
    % (sep, sep, sid, sep, sep, db, sep, int(info.st_mtime), info.st_size)
)
sys.stdout.write(json.dumps(rows, ensure_ascii=False))
PYTHON
  exit $?
fi
error "no Hermes transcript source on this host (no session jsonl, no sqlite3, no python3)" 24
"""


# Follow-up script for a pane whose transcript file is already known: it only
# stats the file and streams the bytes written since the last poll, so a busy
# agent costs "new bytes" instead of the whole 4 MB window every second.
REMOTE_TRANSCRIPT_TAIL_SCRIPT = r"""
file=$1
offset=${2:-0}
pane=$4
cap=${3:-4194304}
[ -f "$file" ] || exit 25
size=$(stat -c '%s' -- "$file" 2>/dev/null || stat -f '%z' -- "$file" 2>/dev/null) || exit 26
stamp=$(stat -c '%Y' -- "$file" 2>/dev/null || stat -f '%m' -- "$file" 2>/dev/null) || exit 26
mode=full
start=1
if [ "$size" -gt "$cap" ]; then start=$((size - cap + 1)); fi
if [ "$offset" -gt 0 ] && [ "$offset" -le "$size" ] && [ $((size - offset)) -le "$cap" ]; then
  mode=append
  start=$((offset + 1))
fi
encoding=none
if command -v gzip >/dev/null 2>&1; then encoding=gzip; fi
truncated=0
if [ "$start" -gt 1 ]; then truncated=1; fi
title=$(${TMUX_BIN:-tmux} display-message -p -t "$pane" '#{pane_title}' 2>/dev/null)
printf 'TMUXWEB_MODE<::>%s\nTMUXWEB_TRUNCATED<::>%s\nTMUXWEB_ENCODING<::>%s\nTMUXWEB_TITLE<::>%s\nTMUXWEB_STAMP<::>%s %s\nTMUXWEB_PAYLOAD\n' \
  "$mode" "$truncated" "$encoding" "$title" "$stamp" "$size"
if [ "$encoding" = gzip ]; then
  tail -c +"$start" -- "$file" 2>/dev/null | gzip -c
else
  tail -c +"$start" -- "$file" 2>/dev/null
fi
"""

# Parsed remote transcripts are cached per (host, pane, limit) so a 1s UI poll
# only costs a tiny stat when nothing changed.
TRANSCRIPT_CACHE_LOCK = threading.Lock()
TRANSCRIPT_CACHE: dict[tuple, dict] = {}
TRANSCRIPT_CACHE_TTL = 2.0
# A poll that costs one ssh round trip should not happen every second on a slow
# host: remember how long the last pull took and reuse the parse for that long.
_TRANSCRIPT_TTL: dict[str, float] = {}


def transcript_ttl(host_id: str) -> float:
    return _TRANSCRIPT_TTL.get(host_id, TRANSCRIPT_CACHE_TTL)


def note_transcript_latency(host_id: str, elapsed: float) -> None:
    _TRANSCRIPT_TTL[host_id] = max(TRANSCRIPT_CACHE_TTL, min(10.0, round(elapsed, 1)))
TRANSCRIPT_CACHE_SIZE = 64


# Per (host, pane) incremental state: where we stopped reading and the bytes we
# have accumulated, so later polls only carry the new tail.
TRANSCRIPT_STATE_LOCK = threading.Lock()
TRANSCRIPT_STATE: dict[tuple, dict] = {}
TRANSCRIPT_STATE_SIZE = 64
TRANSCRIPT_BUFFER_BYTES = 4 * 1024 * 1024


def transcript_state(host_id: str, pane_id: str) -> dict | None:
    with TRANSCRIPT_STATE_LOCK:
        return TRANSCRIPT_STATE.get((host_id, pane_id))


def store_transcript_state(host_id: str, pane_id: str, state: dict) -> None:
    with TRANSCRIPT_STATE_LOCK:
        TRANSCRIPT_STATE[(host_id, pane_id)] = state
        if len(TRANSCRIPT_STATE) > TRANSCRIPT_STATE_SIZE:
            oldest = sorted(TRANSCRIPT_STATE.items(), key=lambda item: item[1].get("at", 0))
            for old_key, _value in oldest[:len(TRANSCRIPT_STATE) - TRANSCRIPT_STATE_SIZE]:
                TRANSCRIPT_STATE.pop(old_key, None)


def drop_transcript_state(host_id: str, pane_id: str) -> None:
    with TRANSCRIPT_STATE_LOCK:
        TRANSCRIPT_STATE.pop((host_id, pane_id), None)


def cache_entry(key: tuple):
    with TRANSCRIPT_CACHE_LOCK:
        return TRANSCRIPT_CACHE.get(key)


def store_cache_entry(key: tuple, entry: dict) -> None:
    with TRANSCRIPT_CACHE_LOCK:
        TRANSCRIPT_CACHE[key] = entry
        if len(TRANSCRIPT_CACHE) > TRANSCRIPT_CACHE_SIZE:
            oldest = sorted(TRANSCRIPT_CACHE.items(), key=lambda item: item[1].get("at", 0))
            for old_key, _value in oldest[:len(TRANSCRIPT_CACHE) - TRANSCRIPT_CACHE_SIZE]:
                TRANSCRIPT_CACHE.pop(old_key, None)


def parse_transcript_response(stdout: bytes) -> tuple[dict, str]:
    """Split the helper's header block from the (possibly gzipped) payload.

    Header lines are plain text; the payload is JSONL, gzipped on the wire when
    the remote host has gzip, because a serialized transcript is mostly repeated
    JSON and the remote uplink is the bottleneck.
    """
    marker = b"\nTMUXWEB_PAYLOAD\n"
    index = stdout.find(marker)
    header_bytes = stdout if index < 0 else stdout[:index]
    header = {}
    for line in header_bytes.decode(errors="replace").splitlines():
        if "<::>" not in line:
            continue
        key, value = line.split("<::>", 1)
        header[key.strip()] = value.strip()
    if header.get("TMUXWEB_ERROR"):
        raise RuntimeError(header["TMUXWEB_ERROR"])
    if index < 0:
        raise RuntimeError("remote transcript response is incomplete")
    payload = stdout[index + len(marker):]
    if header.get("TMUXWEB_ENCODING") == "gzip" and payload:
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise RuntimeError("cannot decode the remote transcript payload: %s" % exc) from None
    return header, payload.decode(errors="replace")


REMOTE_LIST_SCRIPT = r"""
dir=$1
if [ ! -d "$dir" ]; then
  printf 'TMUXWEB_ERROR\tnot a directory: %s\n' "$dir"
  exit 3
fi
printf 'TMUXWEB_PATH\t%s\n' "$(cd "$dir" 2>/dev/null && pwd -P)"
if find -L "$dir" -mindepth 1 -maxdepth 1 -printf '' 2>/dev/null; then
  find -L "$dir" -mindepth 1 -maxdepth 1 -printf '%y\t%s\t%T@\t%p\n'
elif stat -c %s / >/dev/null 2>&1; then
  find -L "$dir" -mindepth 1 -maxdepth 1 -exec stat -c '%F\t%s\t%Y\t%n' {} +
else
  find -L "$dir" -mindepth 1 -maxdepth 1 -exec stat -f '%HT\t%z\t%m\t%N' {} +
fi
"""


def parse_remote_listing(raw: str, directory: str, show_hidden: bool = False,
                        limit: int = 2000) -> dict:
    """Normalise remote find/stat output into the payload the local browser returns."""
    resolved = directory
    entries = []
    for line in raw.splitlines():
        if not line:
            continue
        if line.startswith("TMUXWEB_PATH\t"):
            resolved = line.split("\t", 1)[1]
            continue
        if line.startswith("TMUXWEB_ERROR\t"):
            raise RuntimeError(line.split("\t", 1)[1])
        fields = line.split("\t", 3)
        if len(fields) != 4:
            continue
        kind, size, mtime, path = fields
        name = os.path.basename(path.rstrip("/")) or path
        if not show_hidden and name.startswith("."):
            continue
        lowered = kind.lower()
        if lowered in {"d", "directory"} or lowered.startswith("directory"):
            entry_type = "dir"
            size_value = 0
        elif lowered in {"f", "regular file", "regular empty file"} or lowered.startswith("regular"):
            entry_type = "file"
            size_value = int(size) if size.isdigit() else 0
        elif lowered in {"l", "symbolic link"} or lowered.startswith("symbolic link"):
            entry_type = "link"
            size_value = int(size) if size.isdigit() else 0
        else:
            entry_type = "other"
            size_value = int(size) if size.isdigit() else 0
        try:
            mtime_value = float(mtime)
        except ValueError:
            mtime_value = 0.0
        entries.append({"name": name, "path": path, "type": entry_type, "size": size_value,
                        "mtime": mtime_value, "mode": ""})
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    truncated = len(entries) > limit
    if truncated:
        entries = entries[:limit]
    parent = os.path.dirname(resolved.rstrip("/")) or None
    return {"path": resolved, "parent": parent, "entries": entries, "truncated": truncated}


def ssh_mux_enabled() -> bool:
    return os.environ.get("TMUX_WEB_SSH_MUX", "1").strip().lower() not in {"0", "false", "no", "off"}


def ssh_mux_dir() -> Path:
    """Private directory holding SSH ControlMaster sockets.

    Multiplexing turns every remote tmux poll into a channel on an already
    authenticated connection instead of a fresh TCP + key exchange.
    """
    configured = os.environ.get("TMUX_WEB_SSH_MUX_DIR")
    if configured:
        directory = Path(configured).expanduser()
    else:
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        directory = Path(base) / f"tmuxweb-mux-{os.getuid()}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def ssh_mux_path(host: dict) -> Path:
    token = hashlib.sha256(
        f"{host.get('hostname')}\0{host.get('port')}\0{host.get('username')}".encode()
    ).hexdigest()[:16]
    return ssh_mux_dir() / f"{host.get('id', 'unknown')}-{token}.sock"


def multiplex_failure(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return "control socket" in lowered or "multiplex" in lowered or "controlpath" in lowered


def validate_private_key_text(value: str) -> None:
    text = value.strip()
    if text.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise ValueError("This is a public key, not a private key. Paste the complete OpenSSH private key.")
    valid_headers = (
        "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    )
    if not text.startswith(valid_headers) or "-----END " not in text:
        raise ValueError("Invalid private key format. Paste the complete private key including BEGIN/END lines.")


def host_key_fingerprint(host_key: str) -> str:
    parts = host_key.strip().split()
    if len(parts) < 3:
        raise ValueError("invalid host key")
    blob = base64.b64decode(parts[2], validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return "SHA256:" + digest


def scan_host_key(hostname: str, port: int = 22, timeout: int = 8) -> dict:
    completed = subprocess.run(
        ["ssh-keyscan", "-T", str(timeout), "-p", str(port), "-t", "ed25519,ecdsa,rsa", hostname],
        capture_output=True, text=True, timeout=timeout + 2, check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise RuntimeError(completed.stderr.strip() or "no SSH host key returned")
    preferred = next((line for line in lines if " ssh-ed25519 " in line), lines[0])
    return {"host_key": preferred, "fingerprint": host_key_fingerprint(preferred),
            "algorithm": preferred.split()[1]}


def pane_ref(host_id: str, pane_id: str) -> str:
    if not PANE_ID_RE.fullmatch(pane_id):
        raise ValueError("invalid pane id")
    if host_id == "localhost":
        return pane_id
    if not HOST_ID_RE.fullmatch(host_id):
        raise ValueError("invalid host id")
    return f"{host_id}~{pane_id}"


def split_pane_ref(value: str) -> tuple[str, str]:
    if PANE_ID_RE.fullmatch(value):
        return "localhost", value
    if "~" not in value:
        raise ValueError("invalid pane reference")
    host_id, pane_id = value.split("~", 1)
    if not HOST_ID_RE.fullmatch(host_id) or not PANE_ID_RE.fullmatch(pane_id):
        raise ValueError("invalid pane reference")
    return host_id, pane_id


def parse_tmux_panes(raw: str, host_id: str, separator: str = FIELD_SEPARATOR) -> list[dict]:
    panes = []
    for line in raw.splitlines():
        fields = line.split(separator)
        if len(fields) != 12:
            continue
        pane_id, target, session, window, window_name, index, command, title, path, active, width, height = fields
        if not PANE_ID_RE.fullmatch(pane_id):
            continue
        panes.append({
            "id": pane_ref(host_id, pane_id), "pane_id": pane_id, "host_id": host_id,
            "target": target, "session": session, "window": window, "window_name": window_name,
            "index": index, "command": command, "title": title, "path": path,
            "active": active == "1", "width": int(width), "height": int(height),
        })
    return panes


def _format_jump_spec(parsed: dict) -> str:
    return f"{parsed['username']}@{parsed['hostname']}:{parsed['port']}"


class RemoteHostStore:
    def __init__(self, db_path: str | Path, key_path: str | Path):
        self.db_path = Path(db_path) if db_path != ":memory:" else db_path
        self.key_path = Path(key_path)
        self.fernet = Fernet(self._load_key())
        self.connection = sqlite3.connect(str(db_path), check_same_thread=False)
        # The column holds ciphertext, but this box can be a shared server: keep the
        # database readable by its owner only, like the key file next to it.
        if db_path != ":memory:":
            try:
                Path(db_path).chmod(0o600)
            except OSError:
                pass
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS remote_hosts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT NOT NULL,
                    port INTEGER NOT NULL, username TEXT NOT NULL, auth_type TEXT NOT NULL,
                    credential BLOB NOT NULL, host_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    position INTEGER NOT NULL DEFAULT 0, jump_host_id TEXT NOT NULL DEFAULT '',
                    jump_spec TEXT NOT NULL DEFAULT '', tmux_path TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(remote_hosts)")}
            if "position" not in columns:  # databases from before manual ordering
                self.connection.execute(
                    "ALTER TABLE remote_hosts ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
            if "jump_host_id" not in columns:  # databases from before jump hosts
                self.connection.execute(
                    "ALTER TABLE remote_hosts ADD COLUMN jump_host_id TEXT NOT NULL DEFAULT ''"
                )
            if "jump_spec" not in columns:  # databases from before inline jump hosts
                self.connection.execute(
                    "ALTER TABLE remote_hosts ADD COLUMN jump_spec TEXT NOT NULL DEFAULT ''"
                )
            if "tmux_path" not in columns:  # databases from before the tmux path was remembered
                self.connection.execute(
                    "ALTER TABLE remote_hosts ADD COLUMN tmux_path TEXT NOT NULL DEFAULT ''"
                )

    def _load_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key + b"\n")
        finally:
            os.close(fd)
        return key

    @staticmethod
    def parse_jump_spec(spec: str, *, default_user: str = "", default_port: int = 22) -> dict:
        """Parse ``[user@]host[:port]`` into a hop description.

        This lets a jump host be dialled without first saving it as a host of its
        own; it reuses the target's credential (a bastion that trusts the same key
        or password), which keeps credentials in one place.
        """
        spec = (spec or "").strip()
        if not spec:
            raise ValueError("jump host is empty")
        if "@" in spec:
            user, _, rest = spec.partition("@")
            user = user.strip()
        else:
            user, rest = default_user, spec
        rest = rest.strip()
        hostname, port = rest, int(default_port or 22)
        if rest.startswith("["):  # [ipv6]:port
            closing = rest.find("]")
            if closing < 0:
                raise ValueError("invalid jump host")
            hostname = rest[1:closing]
            tail = rest[closing + 1:]
            if tail.startswith(":"):
                port = int(tail[1:])
        elif rest.count(":") == 1:
            hostname, _, port_text = rest.partition(":")
            if port_text.isdigit():
                port = int(port_text)
            else:
                raise ValueError("invalid jump host port")
        elif rest.count(":") > 1:
            raise ValueError("wrap an IPv6 jump host in [] and add the port")
        hostname = hostname.strip()
        if not hostname or not user:
            raise ValueError("jump host needs a host and a user")
        if not (1 <= port <= 65535):
            raise ValueError("invalid jump host port")
        return {"hostname": hostname, "port": port, "username": user}

    def _checked_jump(self, jump_host_id: str | None, host_id: str = "") -> str:
        """Validate a jump reference: it must exist, not be self, and not loop."""
        jump_host_id = (jump_host_id or "").strip()
        if not jump_host_id:
            return ""
        if jump_host_id == host_id:
            raise ValueError("a host cannot be its own jump host")
        known = {row["id"] for row in self.connection.execute("SELECT id FROM remote_hosts")}
        if jump_host_id not in known:
            raise ValueError("jump host not found")
        seen = {host_id} if host_id else set()
        cursor = jump_host_id
        for _ in range(8):
            if cursor in seen:
                raise ValueError("jump host chain loops")
            seen.add(cursor)
            row = self.connection.execute(
                "SELECT jump_host_id FROM remote_hosts WHERE id = ?", (cursor,)
            ).fetchone()
            cursor = (row["jump_host_id"] if row else "") or ""
            if not cursor:
                return jump_host_id
        raise ValueError("jump host chain is too deep")

    def add_host(self, *, name: str, hostname: str, port: int, username: str,
                 auth_type: str, credential: str, host_key: str, fingerprint: str,
                 jump_host_id: str = "", jump_spec: str = "", tmux_path: str = "") -> dict:
        if auth_type not in AUTH_TYPES:
            raise ValueError("invalid auth type")
        if auth_type in {"password", "key"} and not credential:
            raise ValueError("this auth type needs a credential")
        if auth_type == "agent":
            credential = ""  # the local ssh agent / ~/.ssh keys prove identity
        if auth_type == "key":
            validate_private_key_text(credential)
        host_id = secrets.token_hex(8)
        if (jump_host_id or "").strip() and (jump_spec or "").strip():
            raise ValueError("choose a saved jump host or type one, not both")
        jump_host_id = self._checked_jump(jump_host_id)
        jump_spec = (jump_spec or "").strip()
        if jump_spec:
            parsed = self.parse_jump_spec(jump_spec, default_user=username, default_port=22)
            jump_spec = _format_jump_spec(parsed)
        encrypted = self.fernet.encrypt(credential.encode())
        position = self.connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM remote_hosts"
        ).fetchone()[0]
        with self.connection:
            self.connection.execute(
                "INSERT INTO remote_hosts (id, name, hostname, port, username, auth_type, credential,"
                " host_key, fingerprint, created_at, enabled, position, jump_host_id, jump_spec, tmux_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                (host_id, name, hostname, int(port), username, auth_type, encrypted,
                 host_key, fingerprint, time.time(), position, jump_host_id, jump_spec, tmux_path or ""),
            )
        return self.get_host(host_id)

    def get_host(self, host_id: str, *, include_credential: bool = False) -> dict:
        row = self.connection.execute("SELECT * FROM remote_hosts WHERE id = ?", (host_id,)).fetchone()
        if row is None:
            raise KeyError(host_id)
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        encrypted = result.pop("credential")
        if include_credential:
            result["credential"] = self.fernet.decrypt(encrypted).decode()
        return result

    def list_hosts(self) -> list[dict]:
        return [self.get_host(row["id"]) for row in self.connection.execute(
            "SELECT id FROM remote_hosts ORDER BY position, created_at, name"
        )]

    def update_host(self, host_id: str, *, name: str | None = None, hostname: str | None = None,
                    port: int | None = None, username: str | None = None, auth_type: str | None = None,
                    credential: str | None = None, host_key: str | None = None,
                    fingerprint: str | None = None, jump_host_id: str | None = None,
                    jump_spec: str | None = None, tmux_path: str | None = None) -> dict:
        """Edit a stored host. ``credential=None`` keeps the saved secret."""
        current = self.get_host(host_id, include_credential=True)
        auth = auth_type or current["auth_type"]
        if auth not in AUTH_TYPES:
            raise ValueError("invalid auth type")
        secret = current["credential"] if credential is None else credential
        if auth == "agent":
            secret = ""
        if auth in {"password", "key"} and not secret:
            raise ValueError("password or private key is required")
        if auth == "key":
            validate_private_key_text(secret)
        fields = {
            "name": (name or current["name"]).strip(),
            "hostname": (hostname or current["hostname"]).strip(),
            "port": int(port or current["port"]),
            "username": (username or current["username"]).strip(),
            "auth_type": auth,
            "credential": self.fernet.encrypt(secret.encode()),
            "host_key": (host_key or current["host_key"]),
            "fingerprint": (fingerprint or current["fingerprint"]),
            "jump_host_id": self._checked_jump(
                current.get("jump_host_id", "") if jump_host_id is None else jump_host_id, host_id),
            "jump_spec": (current.get("jump_spec", "") if jump_spec is None else jump_spec or "").strip(),
            "tmux_path": (current.get("tmux_path", "") if tmux_path is None else tmux_path or "").strip(),
        }
        if fields["jump_host_id"] and fields["jump_spec"]:
            raise ValueError("choose a saved jump host or type one, not both")
        if fields["jump_spec"]:
            fields["jump_spec"] = _format_jump_spec(self.parse_jump_spec(
                fields["jump_spec"], default_user=fields["username"], default_port=22))
        if not fields["name"] or not fields["hostname"] or not fields["username"]:
            raise ValueError("name, hostname and username are required")
        with self.connection:
            self.connection.execute(
                "UPDATE remote_hosts SET name=?, hostname=?, port=?, username=?, auth_type=?,"
                " credential=?, host_key=?, fingerprint=?, jump_host_id=?, jump_spec=?, tmux_path=?"
                " WHERE id=?",
                (fields["name"], fields["hostname"], fields["port"], fields["username"],
                 fields["auth_type"], fields["credential"], fields["host_key"], fields["fingerprint"],
                 fields["jump_host_id"], fields["jump_spec"], fields["tmux_path"], host_id),
            )
        return self.get_host(host_id)

    def reorder_hosts(self, ordered_ids: list[str]) -> list[dict]:
        """Persist the sidebar order: the first id becomes position 0."""
        known = {row["id"] for row in self.connection.execute("SELECT id FROM remote_hosts")}
        if sorted(ordered_ids) != sorted(known):
            raise ValueError("the reorder list must contain every host exactly once")
        with self.connection:
            for position, host_id in enumerate(ordered_ids):
                self.connection.execute(
                    "UPDATE remote_hosts SET position=? WHERE id=?", (position, host_id)
                )
        return self.list_hosts()

    def delete_host(self, host_id: str) -> bool:
        with self.connection:
            # hosts that used it as a hop keep working directly instead of dangling
            self.connection.execute(
                "UPDATE remote_hosts SET jump_host_id = '' WHERE jump_host_id = ?", (host_id,)
            )
            cursor = self.connection.execute("DELETE FROM remote_hosts WHERE id = ?", (host_id,))
        return cursor.rowcount > 0

    def jump_chain(self, host: dict, limit: int = 5) -> list[dict]:
        """Inline ``jump_spec`` hop = one entry that reuses this host's credential."""
        """Every hop for a host, nearest first (A -> B -> C gives [B, C], both credentialed)."""
        spec = (host.get("jump_spec") or "").strip()
        if spec and not (host.get("jump_host_id") or "").strip():
            if not host.get("credential") and host.get("id"):
                # the inline hop borrows this host's secret, so it has to be loaded
                try:
                    host = self.get_host(host["id"], include_credential=True)
                except KeyError:
                    pass
            try:
                parsed = self.parse_jump_spec(spec, default_user=host.get("username") or "",
                                              default_port=22)
            except ValueError:
                return []
            return [{
                "id": "",
                "name": spec,
                "hostname": parsed["hostname"],
                "port": parsed["port"],
                "username": parsed["username"],
                # a bastion typed inline trusts the same key/password as the target
                "auth_type": host.get("auth_type") or "agent",
                "credential": host.get("credential") or "",
                "host_key": "",
                "inline": True,
            }]
        chain: list[dict] = []
        seen: set[str] = set()
        current = (host.get("jump_host_id") or "").strip()
        while current and len(chain) < max(1, limit):
            if current in seen:
                LOGGER.warning("jump chain loops at %s", current)
                break
            seen.add(current)
            try:
                hop = self.get_host(current, include_credential=True)
            except KeyError:
                break
            chain.append(hop)
            current = (hop.get("jump_host_id") or "").strip()
        return chain

    def jump_host(self, host: dict) -> dict | None:
        """The first hop for a host (with its credential) or None."""
        chain = self.jump_chain(host, limit=1)
        return chain[0] if chain else None


class SSHRunner:
    def __init__(self, host: dict):
        self.host = host
        # the status sweep sets a short value: it would rather report "unknown"
        # than let one slow host stall the whole sweep
        self.capture_timeout: int | None = None
        # set by the app: persist the path we resolved so later sessions skip the probe
        self.on_tmux_bin = None
        # ``host["jump_chain"]`` is the resolved hops, nearest first, each with its
        # credential (see RemoteHostStore.jump_chain / the app's runner_for());
        # ``host["jump"]`` is kept as the first hop for callers that only need one.
        chain = host.get("jump_chain")
        if chain is None:
            first = host.get("jump")
            chain = [first] if first else []
        self.jump_chain = [hop for hop in chain if hop]
        self.jump = self.jump_chain[0] if self.jump_chain else None
        # set by the app when the stored host has no pinned key yet: it receives
        # the known_hosts line once the first connection succeeds
        self.on_host_key = None

    @staticmethod
    def _write_known_hosts(path: Path, pinned: str) -> Path:
        path.write_text((pinned.strip() + "\n") if pinned.strip() else "")
        path.chmod(0o600)
        return path

    def _known_hosts(self, directory: Path) -> Path:
        return self._write_known_hosts(directory / "known_hosts", self.host.get("host_key") or "")

    def _jump_known_hosts(self, directory: Path, hop: dict | None = None, index: int = 0) -> Path:
        hop = hop if hop is not None else (self.jump or {})
        return self._write_known_hosts(directory / f"known_hosts.jump{index}", hop.get("host_key") or "")

    def jump_description(self) -> str:
        return " -> ".join(f"{hop.get('username')}@{hop.get('hostname')}:{hop.get('port')}"
                           for hop in self.jump_chain)

    def _jump_options(self, *, identity_file: str | None, known_hosts: str,
                      hop: dict | None = None, nested_proxy: str | None = None) -> list[str]:
        """The inner ssh that carries the tunnel (-W %h:%p), optionally through another hop."""
        jump = hop if hop is not None else (self.jump or {})
        pinned = bool((jump.get("host_key") or "").strip())
        options = [
            "ssh", "-W", "%h:%p", "-p", str(jump.get("port") or 22),
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=yes" if pinned else "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "LogLevel=ERROR",
        ]
        if identity_file:
            options += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                        "-o", "PasswordAuthentication=no", "-o", "IdentitiesOnly=yes",
                        "-i", identity_file]
        elif jump.get("auth_type") == "password":
            options += ["-o", "BatchMode=no", "-o", "PreferredAuthentications=password",
                        "-o", "PubkeyAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                        "-o", "NumberOfPasswordPrompts=1"]
        else:
            options += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
        if nested_proxy:
            # this hop is itself reached through the next one: the value is a shell
            # command line, so it has to be quoted before it is embedded
            options += ["-o", f"ProxyCommand={nested_proxy}"]
        options.append(f"{jump.get('username')}@{jump.get('hostname')}")
        return [shlex.quote(option) for option in options]

    def host_key_pinned(self) -> bool:
        return bool((self.host.get("host_key") or "").strip())

    def command(self, remote_command: str, *, tty: bool = False, known_hosts: str = "/dev/null",
                identity_file: str | None = None, multiplex: bool | None = None,
                tmux_bin: str | None = None,
                host_key_pinned: bool = True,
                jump_proxy: str | None = None) -> tuple[list[str], dict[str, str]]:
        # NOTE: ssh takes the *first* value it sees for an option, so BatchMode
        # must be decided per auth mode below - emitting "BatchMode=no" here made
        # agent mode wait on a password prompt instead of failing fast.
        command = [
            "ssh", "-T" if not tty else "-tt", "-p", str(self.host["port"]),
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            # an unpinned host is accepted once, then pinned for every later call
            "-o", "StrictHostKeyChecking=yes" if host_key_pinned else "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "LogLevel=ERROR",
        ]
        if jump_proxy:
            # distinct credentials per hop: ProxyCommand runs its own ssh
            command += ["-o", f"ProxyCommand={jump_proxy}"]
        use_mux = ssh_mux_enabled() if multiplex is None else multiplex
        if use_mux:
            command += [
                "-o", "ControlMaster=auto", "-o", f"ControlPath={ssh_mux_path(self.host)}",
                "-o", "ControlPersist=60",
            ]
        if identity_file:
            command += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                        "-o", "PasswordAuthentication=no", "-o", "IdentitiesOnly=yes", "-i", identity_file]
        elif self.host.get("auth_type") == "password":
            command += ["-o", "BatchMode=no", "-o", "PreferredAuthentications=password",
                        "-o", "PubkeyAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                        "-o", "NumberOfPasswordPrompts=1"]
        else:
            # agent mode: offer the local agent/default keys, never fall back to a prompt
            command += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
        prelude = LOCALE_PRELUDE
        pinned = os.environ.get("TMUX_WEB_REMOTE_PAYLOAD_BYTES", "").strip()
        window = int(pinned) if pinned.isdigit() else payload_window(self.host)
        prelude += f"export TMUXWEB_PAYLOAD_BYTES={window}; "
        if tmux_bin:
            prelude += f"export TMUX_BIN={shlex.quote(tmux_bin)}; "
        command += [f"{self.host['username']}@{self.host['hostname']}", prelude + remote_command]
        environment = ({"SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"}
                       if self.host.get("auth_type") == "password" else {})
        return command, environment

    def tmux_binary(self) -> str:
        """Absolute path of tmux on the remote host (resolved once per host)."""
        configured = os.environ.get("TMUX_WEB_TMUX_PATH", "").strip()
        if configured:
            return configured
        stored = str(self.host.get("tmux_path") or "").strip()
        if stored.startswith("/"):
            # resolved by an earlier session and saved with the host: no probe at all
            _TMUX_BIN_CACHE[tmux_cache_key(self.host)] = stored
            return stored
        key = tmux_cache_key(self.host)
        cached = _TMUX_BIN_CACHE.get(key)
        if cached:
            return cached
        failed_at = _TMUX_BIN_FAILED.get(key)
        if failed_at is not None and (time.monotonic() - failed_at) < _TMUX_BIN_RETRY_AFTER:
            # fall back to a bare `tmux` rather than refusing to talk to the host
            return "tmux"
        try:
            completed = self.run(TMUX_PROBE_SCRIPT, timeout=self.probe_timeout, resolve_tmux=False)
        except RuntimeError as exc:
            # the probe itself stalled: a slow link, not necessarily a missing binary.
            # Fall back to a bare `tmux` (the script's default) and retry the probe later.
            _TMUX_BIN_FAILED[key] = time.monotonic()
            LOGGER.warning("tmux probe timed out on %s: %s", self.host.get("name") or key, exc)
            return "tmux"
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            _TMUX_BIN_FAILED[key] = time.monotonic()
            raise RuntimeError(detail or TMUX_MISSING_HINT)
        path = completed.stdout.decode(errors="replace").strip().splitlines()
        found = path[0].strip() if path else ""
        if not found:
            _TMUX_BIN_FAILED[key] = time.monotonic()
            raise RuntimeError(TMUX_MISSING_HINT)
        _TMUX_BIN_FAILED.pop(key, None)
        _TMUX_BIN_CACHE[key] = found
        if callable(getattr(self, "on_tmux_bin", None)):
            try:
                self.on_tmux_bin(found)
            except (KeyError, ValueError, OSError):
                LOGGER.warning("could not store the tmux path for %s", self.host.get("name") or key)
        return found

    def build_jump_proxy(self, directory: Path) -> tuple[str | None, list[tuple[dict, Path]]]:
        """Prepare every hop's credential files and nest the tunnels.

        Returns (proxy_command, [(hop, password_file), ...]) - the password files
        feed the askpass script, which picks one per hostname.
        """
        if not self.jump_chain:
            return None, []
        passwords: list[tuple[dict, Path]] = []
        # innermost first: the farthest hop is dialled directly
        nested: str | None = None
        for index in range(len(self.jump_chain) - 1, -1, -1):
            hop = self.jump_chain[index]
            identity, password = self._prepare_auth(hop, directory, f"jump{index}.")
            if password:
                passwords.append((hop, password))
            known_hosts = self._jump_known_hosts(directory, hop, index)
            parts = self._jump_options(identity_file=identity, known_hosts=str(known_hosts),
                                       hop=hop, nested_proxy=nested)
            nested = " ".join(parts)
        return nested, list(reversed(passwords))

    def _prepare_auth(self, host: dict, directory: Path, prefix: str) -> tuple[str | None, Path | None]:
        """Write this hop's key or password file; return (identity_file, password_file)."""
        identity_file = None
        password_file = None
        if host.get("auth_type") == "key":
            identity = directory / f"{prefix}identity"
            identity.write_text(host.get("credential") or "")
            identity.chmod(0o600)
            identity_file = str(identity)
        elif host.get("auth_type") == "password":
            password_file = directory / f"{prefix}password"
            password_file.write_text(host.get("credential") or "")
            password_file.chmod(0o600)
        return identity_file, password_file

    def _write_askpass(self, directory: Path, password_file: Path | None,
                       hop_passwords: list[tuple[dict, Path]], askpass: Path) -> None:
        """One askpass for the whole chain: it picks the secret by the hostname in the prompt."""
        if not hop_passwords and password_file is None:
            return
        cases = []
        for hop, path in hop_passwords:
            hostname = str(hop.get("hostname") or "").strip()
            if hostname:
                cases.append(f'  *{hostname}*) cat {shlex.quote(str(path))} ;;')
        if password_file is not None:
            cases.append(f"  *) cat {shlex.quote(str(password_file))} ;;")
        else:
            cases.append("  *) exit 1 ;;")
        askpass.write_text("#!/bin/sh\ncase \"$1\" in\n" + "\n".join(cases) + "\nesac\n")
        askpass.chmod(0o700)

    @property
    def probe_timeout(self) -> int:
        return max(3, int(os.environ.get("TMUX_WEB_PROBE_TIMEOUT", "10")))

    @property
    def transcript_timeout(self) -> int:
        return max(5, int(os.environ.get("TMUX_WEB_TRANSCRIPT_TIMEOUT", "30")))

    def run(self, remote_command: str, *, input_bytes: bytes | None = None,
            timeout: int = 15, resolve_tmux: bool = True) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="tmux-remote-") as directory_name:
            directory = Path(directory_name)
            known_hosts = self._known_hosts(directory)
            identity_file, password_file = self._prepare_auth(self.host, directory, "")
            jump_proxy, hop_passwords = self.build_jump_proxy(directory)
            pass_fds: tuple[int, ...] = ()
            read_fd = write_fd = None
            environment = os.environ.copy()
            if password_file or hop_passwords:
                askpass = directory / "askpass.sh"
                self._write_askpass(directory, password_file, hop_passwords, askpass)
                environment["SSH_ASKPASS"] = str(askpass)
                environment["SSH_ASKPASS_REQUIRE"] = "force"
                environment["DISPLAY"] = ":0"
            tmux_bin = self.tmux_binary() if resolve_tmux else None
            pinned = self.host_key_pinned()
            command, extra_environment = self.command(
                remote_command, known_hosts=str(known_hosts), identity_file=identity_file, tmux_bin=tmux_bin,
                host_key_pinned=pinned, jump_proxy=jump_proxy,
            )
            environment.update(extra_environment)
            if read_fd is not None and write_fd is not None:
                os.write(write_fd, self.host["credential"].encode() + b"\n")
                os.close(write_fd)
            try:
                completed = subprocess.run(command, input=input_bytes, capture_output=True, timeout=timeout,
                                           check=False, env=environment, pass_fds=pass_fds, start_new_session=True)
                if completed.returncode != 0 and multiplex_failure(completed.stderr.decode(errors="replace")):
                    # A stale or unusable ControlMaster socket must never make a
                    # pane unreachable: retry once on a direct connection.
                    direct, _ = self.command(
                        remote_command, known_hosts=str(known_hosts), identity_file=identity_file,
                        multiplex=False, tmux_bin=tmux_bin, host_key_pinned=pinned, jump_proxy=jump_proxy,
                    )
                    completed = subprocess.run(direct, input=input_bytes, capture_output=True, timeout=timeout,
                                               check=False, env=environment, pass_fds=pass_fds,
                                               start_new_session=True)
                if completed.returncode == 0 and not pinned and self.on_host_key:
                    self._remember_host_key(known_hosts)
                return completed
            except subprocess.TimeoutExpired as exc:
                # callers catch RuntimeError/OSError: a stuck ssh must look like a
                # failed command, not escape as an unhandled 500
                raise RuntimeError(f"remote command timed out after {timeout}s") from exc
            finally:
                if read_fd is not None:
                    os.close(read_fd)

    def _remember_host_key(self, path: Path) -> None:
        """Hand the key ssh just accepted to the app so later calls pin it."""
        try:
            lines = [line.strip() for line in path.read_text(errors="replace").splitlines()
                     if line.strip() and not line.startswith("#")]
        except OSError:
            return
        if not lines:
            return
        try:
            self.on_host_key(lines[-1])
        except Exception:  # noqa: BLE001 - pinning must never break the request
            LOGGER.warning("could not remember the host key for %s", self.host.get("name"), exc_info=True)

    def check(self) -> str:
        completed = self.run("printf TMUX_REMOTE_OK", timeout=12)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "SSH connection failed")
        return completed.stdout.decode(errors="replace")

    def create_session(self, name: str, path: str = "") -> dict:
        """Start a detached tmux session on the remote host."""
        name = normalize_session_name(name)
        directory = str(path or "").strip()
        command = f"${{TMUX_BIN:-tmux}} new-session -d -s {shlex.quote(name)}"
        if directory:
            quoted = shlex.quote(directory)
            command = (f"if [ -d {quoted} ]; then ${{TMUX_BIN:-tmux}} new-session -d -s {shlex.quote(name)} "
                       f"-c {quoted}; else echo 'no such directory: {directory}' >&2; exit 3; fi")
        completed = self.run(command)
        detail = completed.stderr.decode(errors="replace").strip()
        if completed.returncode != 0:
            if "duplicate session" in detail:
                raise RuntimeError("session already exists: %s" % name)
            raise RuntimeError(detail or "tmux new-session failed")
        return {"session": name, "path": directory}

    def list_panes(self, timeout: int = 15) -> list[dict]:
        fmt = FIELD_SEPARATOR.join([
            "#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}", "#{session_name}",
            "#{window_index}", "#{window_name}", "#{pane_index}", "#{pane_current_command}",
            "#{pane_title}", "#{pane_current_path}", "#{pane_active}", "#{pane_width}", "#{pane_height}",
        ])
        command = f"${{TMUX_BIN:-tmux}} list-panes -a -F {shlex.quote(fmt)}"
        completed = self.run(command, timeout=timeout)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote tmux failed")
        return parse_tmux_panes(completed.stdout.decode(errors="replace"), self.host["id"])

    def capture_ansi(self, pane_id: str, history: int = 5000) -> str:
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        command = f"${{TMUX_BIN:-tmux}} capture-pane -p -J -e -t {shlex.quote(pane_id)} -S -{max(20, min(history, 5000))}"
        completed = self.run(command, timeout=self.capture_timeout or 15)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote capture failed")
        return completed.stdout.decode(errors="replace")

    def capture_digest(self, pane_id: str, history: int = 5000) -> str:
        """Cheap content digest computed on the remote host.

        The pane text itself never crosses the network while nothing changes,
        which is what makes 0.5s polling affordable over SSH.
        """
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        command = (
            f"${{TMUX_BIN:-tmux}} capture-pane -p -J -e -t {shlex.quote(pane_id)} -S -{max(20, min(history, 5000))} | cksum"
        )
        completed = self.run(command, timeout=20)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote capture failed")
        digest = completed.stdout.decode(errors="replace").strip()
        if not digest:
            raise RuntimeError("remote capture returned no digest")
        return digest

    def snapshot_ansi(self, pane_id: str, history: int = 5000,
                      known_digest: str | None = None) -> tuple[str, str | None]:
        digest = self.capture_digest(pane_id, history)
        if known_digest is not None and digest == known_digest:
            return digest, None
        return digest, self.capture_ansi(pane_id, history)

    def _probe_value(self, paths: list[str]) -> str:
        """Cheap 'did anything change?' fingerprint for the cached transcript files."""
        if not paths:
            return ""
        quoted = " ".join(shlex.quote(path) for path in paths)
        command = (f"stat -c '%Y %s' -- {quoted} 2>/dev/null "
                   f"|| stat -f '%m %z' -- {quoted} 2>/dev/null")
        completed = self.run(command, timeout=15)
        if completed.returncode != 0:
            return ""
        return " ".join(completed.stdout.decode(errors="replace").split())

    @staticmethod
    def _probe_mtime(value: str) -> float:
        tokens = value.split()
        stamps = []
        for index in range(0, len(tokens) - 1, 2):
            try:
                stamps.append(float(tokens[index]))
            except ValueError:
                continue
        return max(stamps) if stamps else 0.0

    def transcript(self, pane_id: str, limit: int = 200, refresh: bool = False) -> dict:
        """Pull a remote transcript's bytes and parse them here, like scp + local parse.

        Repeated polls reuse a cached parse: within the TTL no SSH call happens at
        all, and otherwise a single ``stat`` decides whether the bytes are refetched.
        """
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        limit = max(1, min(int(limit), 1000))
        host_id = self.host.get("id") or ""
        key = (host_id, pane_id, limit)
        if refresh:
            drop_transcript_state(host_id, pane_id)
            with TRANSCRIPT_CACHE_LOCK:
                TRANSCRIPT_CACHE.pop(key, None)
        entry = cache_entry(key)
        now = time.monotonic()
        if entry:
            if now - entry.get("at", 0) < transcript_ttl(host_id):
                return copy.deepcopy(entry["data"])
            if entry.get("probe") and self._probe_value(entry["probe"]) == entry.get("stamp"):
                entry["at"] = now
                data = copy.deepcopy(entry["data"])
                data["mtime"] = self._probe_mtime(entry["stamp"]) or data.get("mtime", 0)
                return data

        state = transcript_state(host_id, pane_id)
        header: dict[str, str] = {}
        payload = b""
        poll_started = time.monotonic()
        if state and state.get("primary"):
            header, payload, state = self._follow_transcript(host_id, pane_id, state)
        if not header:
            command = (f"sh -c {shlex.quote(REMOTE_TRANSCRIPT_SCRIPT)} "
                       f"sh {shlex.quote(pane_id)} {limit}")
            started_at = time.monotonic()
            try:
                completed = self.run(command, timeout=self.transcript_timeout)
            except RuntimeError as exc:
                # a degraded link shows up as a stalled ssh, not as a bad transcript
                raise RuntimeError(
                    f"读取远端 transcript 超时（{self.transcript_timeout}s），可能是到这台主机的网络较慢"
                ) from exc
            note_pull_duration(self.host, time.monotonic() - started_at)
            try:
                header, payload = parse_transcript_response(completed.stdout)
            except RuntimeError:
                if completed.returncode != 0:
                    detail = completed.stderr.decode(errors="replace").strip()
                    if detail:
                        raise RuntimeError(detail) from None
                raise
            drop_transcript_state(host_id, pane_id)
            state = {
                "provider": header.get("TMUXWEB_PROVIDER", ""),
                "format": header.get("TMUXWEB_FORMAT", "jsonl"),
                "session": header.get("TMUXWEB_SESSION", ""),
                "primary": header.get("TMUXWEB_PRIMARY", ""),
                "offset": self._stamp_size(header.get("TMUXWEB_STAMP", "")),
                "buffer": payload,
                "at": now,
                "pane": pane_id,
                "title": header.get("TMUXWEB_TITLE", ""),
                "relocated_at": now,
            }
            if state["primary"]:
                store_transcript_state(host_id, pane_id, state)
        data = transcript_reader.parse_remote_payload(
            header.get("TMUXWEB_PROVIDER", ""), header.get("TMUXWEB_FORMAT", "jsonl"),
            state.get("buffer", payload), limit, header.get("TMUXWEB_SESSION", ""), 0,
        )
        note_transcript_latency(host_id, time.monotonic() - poll_started)
        if not data.get("messages"):
            raise RuntimeError("this session has no messages yet")
        stamp = header.get("TMUXWEB_STAMP", "")
        data["mtime"] = self._probe_mtime(stamp)
        data["pane"] = pane_id
        data.setdefault("host_id", self.host.get("id"))
        primary = header.get("TMUXWEB_PRIMARY", "")
        probe = [primary] if primary else []
        if primary and header.get("TMUXWEB_FORMAT") == "hermes-rows":
            probe.append(primary + "-wal")
        store_cache_entry(key, {"data": copy.deepcopy(data), "at": now, "probe": probe, "stamp": stamp})
        return data

    @staticmethod
    def _stamp_size(stamp: str) -> int:
        tokens = stamp.split()
        if len(tokens) >= 2:
            try:
                return int(tokens[1])
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _relocation_due(state: dict) -> bool:
        """Re-locate the session file periodically, not only when it vanishes."""
        try:
            interval = float(os.environ.get("TMUX_WEB_TRANSCRIPT_RELOCATE", "30"))
        except ValueError:
            interval = 30.0
        if interval <= 0:
            return False
        return time.monotonic() - float(state.get("relocated_at") or 0) > interval

    def _follow_transcript(self, host_id: str, pane_id: str, state: dict) -> tuple[dict, bytes, dict]:
        """Read only the bytes appended since the last poll (or the tail window).

        Returns (header, payload, state); the header is empty when the fast path
        cannot be used and the caller should fall back to full discovery.
        """
        if self._relocation_due(state):
            return {}, b"", state
        command = (f"sh -c {shlex.quote(REMOTE_TRANSCRIPT_TAIL_SCRIPT)} "
                   f"sh {shlex.quote(state['primary'])} {int(state.get('offset') or 0)}"
                   f" 4194304 {shlex.quote(state.get('pane') or '%0')}")
        completed = self.run(command, timeout=60)
        try:
            header, payload = parse_transcript_response(completed.stdout)
        except RuntimeError:
            drop_transcript_state(host_id, pane_id)
            return {}, b"", state
        if header.get("TMUXWEB_ERROR"):
            drop_transcript_state(host_id, pane_id)
            return {}, b"", state
        mode = header.get("TMUXWEB_MODE", "full")
        stamp = header.get("TMUXWEB_STAMP", "")
        current_title = header.get("TMUXWEB_TITLE", "")
        # A new agent session in the same pane writes a new file: the pinned one
        # stops changing, which used to look like a frozen conversation.
        if current_title and state.get("title") and current_title != state["title"]:
            drop_transcript_state(host_id, pane_id)
            return {}, b"", state
        offset = self._stamp_size(stamp)
        if mode == "append":
            merged = state.get("remainder", "") + payload
            cut = merged.rfind("\n") + 1
            complete, remainder = merged[:cut], merged[cut:]
            buffer = (state.get("buffer", "") + complete)[-TRANSCRIPT_BUFFER_BYTES:]
        elif header.get("TMUXWEB_TRUNCATED") == "1":
            # the window started mid-file, so drop the leading partial line
            cut = payload.find("\n") + 1
            buffer, remainder = (payload[cut:], "") if cut > 0 else ("", payload)
        else:
            buffer, remainder = payload, ""
        state = {**state, "offset": offset, "buffer": buffer, "remainder": remainder,
                 "at": time.monotonic(), "relocated_at": time.monotonic()}
        store_transcript_state(host_id, pane_id, state)
        header = {
            "TMUXWEB_PROVIDER": state.get("provider", ""),
            "TMUXWEB_FORMAT": state.get("format", "jsonl"),
            "TMUXWEB_SESSION": state.get("session", ""),
            "TMUXWEB_PRIMARY": state.get("primary", ""),
            "TMUXWEB_STAMP": stamp,
        }
        return header, b"", state

    def list_files(self, directory: str, show_hidden: bool = False, limit: int = 2000) -> dict:
        """List a remote directory with POSIX tools only (no python3 needed)."""
        if "\x00" in directory or "\n" in directory:
            raise ValueError("invalid directory")
        command = f"sh -c {shlex.quote(REMOTE_LIST_SCRIPT)} sh {shlex.quote(directory)}"
        completed = self.run(command, timeout=45)
        stdout = completed.stdout.decode(errors="replace")
        if completed.returncode != 0 and "TMUXWEB_ERROR" not in stdout:
            detail = completed.stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or "remote directory listing failed")
        payload = parse_remote_listing(stdout, directory, show_hidden,
                                       max(1, min(int(limit), 5000)))
        payload["host_id"] = self.host.get("id")
        return payload

    def download_file(self, path: str, max_bytes: int) -> bytes:
        """Read a remote file; the size limit is enforced before the transfer."""
        if "\x00" in path or "\n" in path:
            raise ValueError("invalid path")
        quoted = shlex.quote(path)
        # distingish "missing" from "unreadable" so a stale link is a 404
        command = (f"if [ ! -f {quoted} ]; then echo 'no such file: {path}' >&2; exit 44; fi; "
                   f"head -c {int(max_bytes) + 1} -- {quoted}")
        completed = self.run(command, timeout=120)
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or "remote file could not be read")
        if len(completed.stdout) > max_bytes:
            raise RuntimeError("file is larger than the download limit")
        return completed.stdout

    def zip_files(self, paths: list[str], base: str, max_bytes: int, max_files: int) -> bytes:
        """Pull a ``tar`` stream in one SSH call and re-container it as a zip.

        Remote hosts then only need ``tar`` (universal) instead of ``zip``.
        """
        if not paths:
            raise ValueError("no files selected")
        base_path = base.rstrip("/") or "/"
        items = []
        for raw in paths:
            cleaned = raw.rstrip("/") or "/"
            if cleaned == base_path or cleaned.startswith(base_path + "/"):
                items.append(cleaned[len(base_path):].lstrip("/") or ".")
            else:
                items.append(cleaned)
        quoted = " ".join(shlex.quote(item) for item in items)
        # Fail before transferring gigabytes: one cheap `du` tells us the size.
        measure = (f"cd {shlex.quote(base_path)} && du -sk {quoted} 2>/dev/null "
                   f"| awk '{{s+=$1}} END {{print s+0}}'")
        try:
            measured = self.run(measure, timeout=20)
        except subprocess.TimeoutExpired:
            measured = None  # sizing a huge tree is not worth blocking the request
        try:
            kilobytes = int((measured.stdout.decode(errors="replace").strip() if measured else "") or 0)
        except ValueError:
            kilobytes = 0
        if kilobytes and kilobytes * 1024 > max_bytes:
            raise RuntimeError("selection is larger than the %d byte zip limit (%d KB on disk)"
                               % (max_bytes, kilobytes))
        command = (f"tar -cf - -C {shlex.quote(base_path)} -- {quoted} 2>/dev/null "
                   f"| head -c {int(max_bytes) + 1}")
        try:
            completed = self.run(command, timeout=int(os.environ.get("TMUX_WEB_ZIP_TIMEOUT", "180")))
        except subprocess.TimeoutExpired:
            raise RuntimeError("打包超时：选区太大或远端链路太慢，请减少文件后重试")
        payload = completed.stdout
        if not payload:
            detail = completed.stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or "the remote host returned no archive data")
        if len(payload) > max_bytes:
            raise RuntimeError("selection is larger than the %d byte zip limit" % max_bytes)
        return file_browser.tar_to_zip(payload, max_bytes=max_bytes, max_files=max_files)

    def upload_file(self, path: str, data: bytes) -> None:
        """Write a remote file from raw bytes, creating the parent directory."""
        if "\x00" in path or "\n" in path:
            raise ValueError("invalid path")
        directory = os.path.dirname(path) or "."
        command = f"mkdir -p -- {shlex.quote(directory)} && cat > {shlex.quote(path)}"
        completed = self.run(command, input_bytes=data, timeout=180)
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or "remote file could not be written")

    def capture(self, pane_id: str, history: int = 5000) -> str:
        return ANSI_RE.sub("", self.capture_ansi(pane_id, history))

    def capture_many(self, pane_ids, history: int = 40) -> dict:
        """Capture several panes in one ssh call: pane text keyed by pane id.

        The status sweep needs a short tail of every pane on a host; doing that one
        ssh per pane made a 35-pane macmini cost 35 round trips.
        """
        ids = [pane for pane in pane_ids if PANE_ID_RE.fullmatch(str(pane or ""))]
        if not ids:
            return {}
        ids = ids[:80]
        lines = max(20, min(int(history), 5000))
        script = "for pane in " + " ".join(shlex.quote(pane) for pane in ids) + "; do " \
                 "printf '\\036%s\\n' \"$pane\"; " \
                 "${TMUX_BIN:-tmux} capture-pane -p -J -t \"$pane\" -S -" + str(lines) + " 2>/dev/null; done"
        completed = self.run(script, timeout=self.capture_timeout or 15)
        if completed.returncode != 0 and not completed.stdout:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote capture failed")
        result = {}
        current = None
        buffer = []
        for line in completed.stdout.decode(errors="replace").split("\n"):
            if line.startswith("\x1e"):
                if current is not None:
                    result[current] = ANSI_RE.sub("", "\n".join(buffer))
                current = line[1:].strip()
                buffer = []
            elif current is not None:
                buffer.append(line)
        if current is not None:
            result[current] = ANSI_RE.sub("", "\n".join(buffer))
        return result

    def send_key(self, pane_id: str, key: str) -> None:
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        command = f"${{TMUX_BIN:-tmux}} send-keys -t {shlex.quote(pane_id)} {shlex.quote(key)}"
        completed = self.run(command)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote key send failed")

    def send(self, pane_id: str, text: str, submit: bool = True) -> None:
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        name = "web-" + secrets.token_hex(6)
        clear = f"${{TMUX_BIN:-tmux}} send-keys -t {shlex.quote(pane_id)} C-u; sleep 0.05; " if submit else ""
        submit_cmd = f"; sleep 0.15; ${{TMUX_BIN:-tmux}} send-keys -t {shlex.quote(pane_id)} C-m" if submit else ""
        command = clear + f"${{TMUX_BIN:-tmux}} load-buffer -b {name} -; ${{TMUX_BIN:-tmux}} paste-buffer -b {name} -d -t {shlex.quote(pane_id)}" + submit_cmd
        completed = self.run(command, input_bytes=text.encode())
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote send failed")


class RemoteTerminalSession:
    def __init__(self, host: dict, pane_id: str, cols: int, rows: int):
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        self.host = host
        self.pane_id = pane_id
        self.tempdir = tempfile.TemporaryDirectory(prefix="tmux-remote-pty-")
        directory = Path(self.tempdir.name)
        runner = SSHRunner(host)
        known_hosts = runner._known_hosts(directory)
        identity_file, password_file = runner._prepare_auth(host, directory, "")
        jump_proxy, hop_passwords = runner.build_jump_proxy(directory)
        environment = os.environ.copy()
        if password_file or hop_passwords:
            askpass = directory / "askpass.sh"
            runner._write_askpass(directory, password_file, hop_passwords, askpass)
            environment.update({"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"})
        # `${TMUX_BIN:-tmux}` is only right when the variable is exported: on macOS a
        # non-interactive ssh has no /opt/homebrew/bin on PATH, so passing the resolved
        # path here is what keeps `attach-session` from failing with "command not found".
        try:
            tmux_bin = runner.tmux_binary()
        except RuntimeError:
            tmux_bin = ""
        # A phone-sized attach leaves the window small once it detaches (with no client
        # left tmux keeps the last size), which then breaks the next viewer. Remember the
        # size here, attach, and put it back afterwards -- the remote shell keeps running
        # because our ssh closing turns into EOF on the attached pty.
        quoted = shlex.quote(pane_id)
        marker = re.sub(r"[^0-9A-Za-z]", "", pane_id) or "pane"
        remote_command = (
            f"TM=${{TMUX_BIN:-tmux}}; S={quoted}; F=/tmp/.tmuxweb-geom-{marker}; "
            f'prev=$($TM display -p -t "$S" "#{{window_width}}x#{{window_height}}" 2>/dev/null); '
            f'SN=$($TM display -p -t "$S" "#{{session_name}}" 2>/dev/null); '
            f'ds=$($TM show-options -t "$S" -v default-size 2>/dev/null); '
            f'printf \'%s\\n%s\\n\' "$prev" "$ds" > "$F" 2>/dev/null; '
            f'$TM attach-session -t "$S"; rc=$?; '
            f'if [ -n "$prev" ] && [ -z "$($TM list-clients -t "$S" -F . 2>/dev/null)" ]; then '
            f'[ -n "$SN" ] && $TM set-option -t "$SN" default-size "$prev" 2>/dev/null; '
            f'$TM resize-window -t "$S" -x "${{prev%%x*}}" -y "${{prev##*x}}" 2>/dev/null; '
            f'$TM set-option -w -u -t "$S" window-size 2>/dev/null; '
            f'[ -n "$ds" ] && [ -n "$SN" ] && $TM set-option -t "$SN" default-size "$ds" 2>/dev/null; fi; '
            f'rm -f "$F" 2>/dev/null; '
            f'exit $rc'
        )
        command, extra = runner.command(remote_command, tty=True, known_hosts=str(known_hosts),
                                        identity_file=identity_file, jump_proxy=jump_proxy,
                                        tmux_bin=tmux_bin or None)
        environment.update(extra)
        environment["TERM"] = "xterm-256color"
        self.master_fd, slave_fd = os.openpty()
        self.process = subprocess.Popen(command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                        env=environment, close_fds=True, start_new_session=True)
        os.close(slave_fd)
        self._closed = False
        self._set_size(cols, rows)

    def _set_size(self, cols: int, rows: int):
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 300))
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def read(self):
        try: return await asyncio.to_thread(os.read, self.master_fd, 65536)
        except OSError: return b""

    async def write(self, data: bytes):
        if data: await asyncio.to_thread(os.write, self.master_fd, data)

    async def resize(self, cols: int, rows: int):
        await asyncio.to_thread(self._set_size, cols, rows)

    def restore_geometry(self):
        """Undo the window size our client imposed, over a fresh connection.

        Killing the ssh client can take the remote shell down before it reaches its own
        restore step, so the attach also drops the original size into a marker file and
        this runs the restore separately.
        """
        marker = re.sub(r"[^0-9A-Za-z]", "", self.pane_id) or "pane"
        quoted = shlex.quote(self.pane_id)
        script = (
            f"TM=${{TMUX_BIN:-tmux}}; S={quoted}; F=/tmp/.tmuxweb-geom-{marker}; "
            f'[ -f "$F" ] || exit 0; '
            f'prev=$(sed -n 1p "$F" 2>/dev/null); ds=$(sed -n 2p "$F" 2>/dev/null); '
            f'SN=$($TM display -p -t "$S" "#{{session_name}}" 2>/dev/null); '
            f'if [ -n "$prev" ] && [ -n "$SN" ] && [ -z "$($TM list-clients -t "$S" -F . 2>/dev/null)" ]; then '
            f'$TM set-option -t "$SN" default-size "$prev" 2>/dev/null; '
            f'$TM resize-window -t "$S" -x "${{prev%%x*}}" -y "${{prev##*x}}" 2>/dev/null; '
            f'$TM set-option -w -u -t "$S" window-size 2>/dev/null; '
            f'[ -n "$ds" ] && $TM set-option -t "$SN" default-size "$ds" 2>/dev/null; fi; '
            f'rm -f "$F" 2>/dev/null; exit 0'
        )
        try:
            runner = SSHRunner(self.host)
            directory = Path(self.tempdir.name)
            known_hosts = runner._known_hosts(directory)
            identity_file, password_file = runner._prepare_auth(self.host, directory, "")
            jump_proxy, hop_passwords = runner.build_jump_proxy(directory)
            environment = os.environ.copy()
            if password_file or hop_passwords:
                askpass = directory / "askpass.sh"
                runner._write_askpass(directory, password_file, hop_passwords, askpass)
                environment.update({"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"})
            command, extra = runner.command(script, tty=False, known_hosts=str(known_hosts),
                                            identity_file=identity_file, jump_proxy=jump_proxy,
                                            tmux_bin=None)
            environment.update(extra)
            subprocess.run(command, capture_output=True, env=environment, timeout=20,
                           stdin=subprocess.DEVNULL)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass

    async def close(self):
        if self._closed: return
        self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try: await asyncio.to_thread(self.process.wait, 2)
            except subprocess.TimeoutExpired:
                self.process.kill(); await asyncio.to_thread(self.process.wait)
        try: os.close(self.master_fd)
        except OSError: pass
        await asyncio.to_thread(self.restore_geometry)
        self.tempdir.cleanup()
