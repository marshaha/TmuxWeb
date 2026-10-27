from __future__ import annotations

import base64
import copy
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
LOCALE_PRELUDE = "export LC_ALL=${TMUXWEB_REMOTE_LC:-C.UTF-8} LANG=${TMUXWEB_REMOTE_LC:-C.UTF-8}; "
# Remote panes are read the same way ``scp`` would: a POSIX shell snippet finds
# the provider's transcript file (or exports rows with sqlite3 for Hermes) and
# the bytes are pulled here and parsed by transcript_reader. No remote python.
REMOTE_TRANSCRIPT_SCRIPT = r"""
pane=$1
limit=${2:-200}
sep='<::>'
header() {
  printf 'TMUXWEB_PROVIDER%s%s\nTMUXWEB_SESSION%s%s\nTMUXWEB_FORMAT%s%s\n' \
    "$sep" "$provider" "$sep" "$1" "$sep" "$2"
  if [ -n "$3" ] && [ -e "$3" ]; then
    printf 'TMUXWEB_PRIMARY%s%s\n' "$sep" "$3"
    stamps=$(stat -c '%Y %s' -- "$3" 2>/dev/null || stat -f '%m %z' -- "$3" 2>/dev/null)
    printf 'TMUXWEB_STAMP%s%s\n' "$sep" "$stamps"
  fi
  printf 'TMUXWEB_PAYLOAD\n'
}
error() { printf 'TMUXWEB_ERROR%s%s\n' "$sep" "$1"; exit "${2:-20}"; }
payload_bytes=4194304

info=$(tmux display-message -p -t "$pane" \
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
  if [ -n "$title" ]; then
    matches=$(grep -lF -- "$title" "$proj"/*.jsonl 2>/dev/null | head -2)
    [ -n "$matches" ] && [ "$(printf '%s\n' "$matches" | grep -c .)" -eq 1 ] && file=$matches
  fi
  if [ -z "$file" ]; then
    probes=$(tmux capture-pane -p -J -t "$pane" -S -80 2>/dev/null | awk 'length($0) >= 24' | tail -40)
    while IFS= read -r probe; do
      [ -n "$probe" ] || continue
      matches=$(grep -lF -e "$probe" "$proj"/*.jsonl 2>/dev/null | head -2)
      if [ -n "$matches" ] && [ "$(printf '%s\n' "$matches" | grep -c .)" -eq 1 ]; then
        file=$matches
        break
      fi
    done <<PROBES
$probes
PROBES
  fi
  [ -n "$file" ] || file=$(ls -t "$proj"/*.jsonl 2>/dev/null | head -1)
  [ -n "$file" ] || error "no Claude transcript for $cwd" 23
  header "$(basename "$file" .jsonl)" jsonl "$file"
  tail -c "$payload_bytes" -- "$file" 2>/dev/null
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
  tail -c "$payload_bytes" -- "$file" 2>/dev/null
  exit 0
fi

if [ "$provider" = kimi ]; then
  file=""
  id=""
  fallback=""
  fallback_id=""
  for state in "$HOME"/.kimi-code/sessions/wd_*/session_*/state.json; do
    [ -f "$state" ] || continue
    grep -qF -- "$cwd" "$state" 2>/dev/null || continue
    grep -qF '"archived": true' "$state" 2>/dev/null && continue
    wire=$(dirname "$state")/agents/main/wire.jsonl
    [ -f "$wire" ] || continue
    if [ -n "$title" ] && grep -qF -- "$title" "$state" 2>/dev/null; then
      file=$wire
      id=$(basename "$(dirname "$state")")
      break
    fi
    if [ -z "$fallback" ]; then
      fallback=$wire
      fallback_id=$(basename "$(dirname "$state")")
    fi
  done
  [ -n "$file" ] || file=$fallback
  [ -n "$file" ] || error "no Kimi transcript for $cwd" 23
  [ -n "$id" ] || id=$fallback_id
  header "$id" jsonl "$file"
  tail -c "$payload_bytes" -- "$file" 2>/dev/null
  exit 0
fi

# hermes: prefer the per-session jsonl the CLI keeps, else query state.db
pane_pid=$(tmux display-message -p -t "$pane" '#{pane_pid}' 2>/dev/null)
sid=""
for candidate in $pane_pid $(pgrep -P "$pane_pid" 2>/dev/null) \
    $(for child in $(pgrep -P "$pane_pid" 2>/dev/null); do pgrep -P "$child" 2>/dev/null; done); do
  sid=$(tr '\0' ' ' < "/proc/$candidate/cmdline" 2>/dev/null \
    | sed -n 's/.*--resume[ =]\([0-9A-Za-z][0-9A-Za-z_-]*\).*/\1/p')
  [ -n "$sid" ] && break
done
if [ -n "$sid" ] && [ -f "$HOME/.hermes/sessions/$sid.jsonl" ]; then
  header "$sid" hermes-jsonl "$HOME/.hermes/sessions/$sid.jsonl"
  tail -c "$payload_bytes" -- "$HOME/.hermes/sessions/$sid.jsonl" 2>/dev/null
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


# Parsed remote transcripts are cached per (host, pane, limit) so a 1s UI poll
# only costs a tiny stat when nothing changed.
TRANSCRIPT_CACHE_LOCK = threading.Lock()
TRANSCRIPT_CACHE: dict[tuple, dict] = {}
TRANSCRIPT_CACHE_TTL = 2.0
TRANSCRIPT_CACHE_SIZE = 64


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
    """Split the helper's header block from the raw transcript payload."""
    text = stdout.decode(errors="replace")
    marker = "\nTMUXWEB_PAYLOAD\n"
    index = text.find(marker)
    header = {}
    for line in (text if index < 0 else text[:index]).splitlines():
        if "<::>" not in line:
            continue
        key, value = line.split("<::>", 1)
        header[key.strip()] = value.strip()
    if header.get("TMUXWEB_ERROR"):
        raise RuntimeError(header["TMUXWEB_ERROR"])
    if index < 0:
        raise RuntimeError("remote transcript response is incomplete")
    return header, text[index + len(marker):]


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
                    created_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                )
            """)

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

    def add_host(self, *, name: str, hostname: str, port: int, username: str,
                 auth_type: str, credential: str, host_key: str, fingerprint: str) -> dict:
        if auth_type not in AUTH_TYPES:
            raise ValueError("invalid auth type")
        if auth_type in {"password", "key"} and not credential:
            raise ValueError("this auth type needs a credential")
        if auth_type == "agent":
            credential = ""  # the local ssh agent / ~/.ssh keys prove identity
        if auth_type == "key":
            validate_private_key_text(credential)
        host_id = secrets.token_hex(8)
        encrypted = self.fernet.encrypt(credential.encode())
        with self.connection:
            self.connection.execute(
                "INSERT INTO remote_hosts VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (host_id, name, hostname, int(port), username, auth_type, encrypted,
                 host_key, fingerprint, time.time()),
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
            "SELECT id FROM remote_hosts ORDER BY created_at, name"
        )]

    def delete_host(self, host_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute("DELETE FROM remote_hosts WHERE id = ?", (host_id,))
        return cursor.rowcount > 0


class SSHRunner:
    def __init__(self, host: dict):
        self.host = host

    def _known_hosts(self, directory: Path) -> Path:
        path = directory / "known_hosts"
        path.write_text(self.host["host_key"].rstrip() + "\n")
        path.chmod(0o600)
        return path

    def command(self, remote_command: str, *, tty: bool = False, known_hosts: str = "/dev/null",
                identity_file: str | None = None, multiplex: bool | None = None) -> tuple[list[str], dict[str, str]]:
        command = [
            "ssh", "-T" if not tty else "-tt", "-p", str(self.host["port"]),
            "-o", "BatchMode=no", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "LogLevel=ERROR",
        ]
        use_mux = ssh_mux_enabled() if multiplex is None else multiplex
        if use_mux:
            command += [
                "-o", "ControlMaster=auto", "-o", f"ControlPath={ssh_mux_path(self.host)}",
                "-o", "ControlPersist=60",
            ]
        if identity_file:
            command += ["-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
                        "-o", "IdentitiesOnly=yes", "-i", identity_file]
        elif self.host.get("auth_type") == "password":
            command += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                        "-o", "KbdInteractiveAuthentication=no", "-o", "NumberOfPasswordPrompts=1"]
        else:
            # agent mode: offer the local agent/default keys, never fall back to a prompt
            command += ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
        command += [f"{self.host['username']}@{self.host['hostname']}", LOCALE_PRELUDE + remote_command]
        environment = ({"SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"}
                       if self.host.get("auth_type") == "password" else {})
        return command, environment

    def run(self, remote_command: str, *, input_bytes: bytes | None = None,
            timeout: int = 15) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="tmux-remote-") as directory_name:
            directory = Path(directory_name)
            known_hosts = self._known_hosts(directory)
            identity_file = None
            pass_fds: tuple[int, ...] = ()
            read_fd = write_fd = None
            environment = os.environ.copy()
            if self.host["auth_type"] == "key":
                identity = directory / "identity"
                identity.write_text(self.host["credential"])
                identity.chmod(0o600)
                identity_file = str(identity)
            elif self.host["auth_type"] == "password":
                password_file = directory / "password"
                password_file.write_text(self.host["credential"])
                password_file.chmod(0o600)
                askpass = directory / "askpass.sh"
                askpass.write_text(f"#!/bin/sh\ncat {shlex.quote(str(password_file))}\n")
                askpass.chmod(0o700)
                environment["SSH_ASKPASS"] = str(askpass)
                environment["SSH_ASKPASS_REQUIRE"] = "force"
                environment["DISPLAY"] = ":0"
            command, extra_environment = self.command(
                remote_command, known_hosts=str(known_hosts), identity_file=identity_file
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
                        multiplex=False,
                    )
                    completed = subprocess.run(direct, input=input_bytes, capture_output=True, timeout=timeout,
                                               check=False, env=environment, pass_fds=pass_fds,
                                               start_new_session=True)
                return completed
            finally:
                if read_fd is not None:
                    os.close(read_fd)

    def check(self) -> str:
        completed = self.run("printf TMUX_REMOTE_OK", timeout=12)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "SSH connection failed")
        return completed.stdout.decode(errors="replace")

    def create_session(self, name: str, path: str = "") -> dict:
        """Start a detached tmux session on the remote host."""
        name = normalize_session_name(name)
        directory = str(path or "").strip()
        command = f"tmux new-session -d -s {shlex.quote(name)}"
        if directory:
            quoted = shlex.quote(directory)
            command = (f"if [ -d {quoted} ]; then tmux new-session -d -s {shlex.quote(name)} "
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
        command = f"tmux list-panes -a -F {shlex.quote(fmt)}"
        completed = self.run(command, timeout=timeout)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "remote tmux failed")
        return parse_tmux_panes(completed.stdout.decode(errors="replace"), self.host["id"])

    def capture_ansi(self, pane_id: str, history: int = 5000) -> str:
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        command = f"tmux capture-pane -p -J -e -t {shlex.quote(pane_id)} -S -{max(20, min(history, 5000))}"
        completed = self.run(command)
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
            f"tmux capture-pane -p -J -e -t {shlex.quote(pane_id)} -S -{max(20, min(history, 5000))} | cksum"
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

    def transcript(self, pane_id: str, limit: int = 200) -> dict:
        """Pull a remote transcript's bytes and parse them here, like scp + local parse.

        Repeated polls reuse a cached parse: within the TTL no SSH call happens at
        all, and otherwise a single ``stat`` decides whether the bytes are refetched.
        """
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        limit = max(1, min(int(limit), 1000))
        key = (self.host.get("id") or "", pane_id, limit)
        entry = cache_entry(key)
        now = time.monotonic()
        if entry:
            if now - entry.get("at", 0) < TRANSCRIPT_CACHE_TTL:
                return copy.deepcopy(entry["data"])
            if entry.get("probe") and self._probe_value(entry["probe"]) == entry.get("stamp"):
                entry["at"] = now
                data = copy.deepcopy(entry["data"])
                data["mtime"] = self._probe_mtime(entry["stamp"]) or data.get("mtime", 0)
                return data

        command = (f"sh -c {shlex.quote(REMOTE_TRANSCRIPT_SCRIPT)} "
                   f"sh {shlex.quote(pane_id)} {limit}")
        completed = self.run(command, timeout=90)
        try:
            header, payload = parse_transcript_response(completed.stdout)
        except RuntimeError:
            if completed.returncode != 0:
                detail = completed.stderr.decode(errors="replace").strip()
                if detail:
                    raise RuntimeError(detail) from None
            raise
        data = transcript_reader.parse_remote_payload(
            header.get("TMUXWEB_PROVIDER", ""), header.get("TMUXWEB_FORMAT", "jsonl"),
            payload, limit, header.get("TMUXWEB_SESSION", ""), 0,
        )
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
        command = f"head -c {int(max_bytes) + 1} -- {shlex.quote(path)} 2>/dev/null"
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

    def send(self, pane_id: str, text: str, submit: bool = True) -> None:
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        name = "web-" + secrets.token_hex(6)
        clear = f"tmux send-keys -t {shlex.quote(pane_id)} C-u; sleep 0.05; " if submit else ""
        submit_cmd = f"; sleep 0.15; tmux send-keys -t {shlex.quote(pane_id)} C-m" if submit else ""
        command = clear + f"tmux load-buffer -b {name} -; tmux paste-buffer -b {name} -d -t {shlex.quote(pane_id)}" + submit_cmd
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
        identity_file = None
        environment = os.environ.copy()
        if host["auth_type"] == "key":
            identity = directory / "identity"
            identity.write_text(host["credential"])
            identity.chmod(0o600)
            identity_file = str(identity)
        elif host["auth_type"] == "password":
            password_file = directory / "password"
            password_file.write_text(host["credential"])
            password_file.chmod(0o600)
            askpass = directory / "askpass.sh"
            askpass.write_text(f"#!/bin/sh\ncat {shlex.quote(str(password_file))}\n")
            askpass.chmod(0o700)
            environment.update({"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"})
        remote_command = f"exec tmux attach-session -t {shlex.quote(pane_id)}"
        command, extra = runner.command(remote_command, tty=True, known_hosts=str(known_hosts), identity_file=identity_file)
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
        self.tempdir.cleanup()
