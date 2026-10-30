from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import importlib.util
import json
import logging
import mimetypes
import os
import fcntl
import re
import secrets
import signal
import struct
import subprocess
import termios
import sqlite3

from auth_store import PasswordStore
import time
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from html import escape
from urllib.parse import parse_qs, quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
import file_browser
from home_store import HomeStore
import mcp_relay
import agent_status
import relay
import transcript_reader
from remote_hosts import (AUTH_TYPES, FIELD_SEPARATOR, RemoteHostStore, RemoteTerminalSession, SSHRunner,
                          host_key_fingerprint, normalize_session_name, pane_ref, scan_host_key, split_pane_ref,
                          validate_private_key_text)

LOGGER = logging.getLogger("tmux_web")

PANE_RE = re.compile(r"^%[0-9]+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
def safe_next_url(value: str, default: str = "/") -> str:
    """Only a same-origin path may be used as a post-login target."""
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if any(char in candidate for char in ("\\", "\r", "\n", "\t")):
        return default
    return candidate


COOKIE_NAME = "tmux_web_session"
SHARE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"


class LoginRateLimiter:
    def __init__(self, attempts: int = 5, window: int = 60):
        self.attempts = attempts
        self.window = window
        self.failures = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            queue = self.failures[key]
            while queue and queue[0] <= now - self.window:
                queue.popleft()
            if len(queue) >= self.attempts:
                raise HTTPException(status_code=429, detail="too many login attempts", headers={"Retry-After": str(self.window)})

    def fail(self, key: str) -> None:
        with self.lock:
            self.failures[key].append(time.monotonic())

    def success(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


class TmuxError(RuntimeError):
    pass


class SnapshotHistory:
    def __init__(self, max_lines: int = 20_000):
        self.max_lines = max_lines
        self._lines: dict[str, list[str]] = {}
        self._screens: dict[str, list[str]] = {}

    def update(self, pane_id: str, content: str) -> str:
        screen = content.splitlines()
        previous = self._screens.get(pane_id, [])
        accumulated = self._lines.get(pane_id, [])
        if not accumulated:
            accumulated = list(screen)
        elif screen != previous:
            overlap = 0
            maximum = min(len(previous), len(screen))
            for size in range(maximum, 0, -1):
                if previous[-size:] == screen[:size]:
                    overlap = size
                    break
            if overlap:
                accumulated.extend(screen[overlap:])
            else:
                accumulated.extend(["", "──────── screen update ────────", *screen])
        if len(accumulated) > self.max_lines:
            accumulated = accumulated[-self.max_lines:]
        self._lines[pane_id] = accumulated
        self._screens[pane_id] = screen
        return "\n".join(accumulated) + ("\n" if accumulated else "")


class TmuxBackend:
    @staticmethod
    def _reverse_lines(path: Path, chunk_size: int = 64 * 1024, max_line: int = 8 * 1024 * 1024):
        return transcript_reader.reverse_lines(path, chunk_size, max_line)

    @staticmethod
    def _run(*args: str, input_bytes: bytes | None = None) -> str:
        completed = subprocess.run(
            ["tmux", *args], input=input_bytes, capture_output=True, check=False
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            raise TmuxError(detail or "tmux command failed")
        return completed.stdout.decode(errors="replace")

    def create_session(self, name: str, path: str = "") -> dict:
        try:
            name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        directory = str(path or "").strip()
        args = ["new-session", "-d", "-s", name]
        if directory:
            resolved = Path(directory).expanduser()
            if not resolved.is_dir():
                raise TmuxError("no such directory: %s" % directory)
            directory = str(resolved)
            args += ["-c", directory]
        try:
            self._run(*args)
        except TmuxError as exc:
            if "duplicate session" in str(exc):
                raise TmuxError("session already exists: %s" % name) from exc
            raise
        return {"session": name, "path": directory}

    def list_panes(self) -> list[dict]:
        format_string = FIELD_SEPARATOR.join([
            "#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}",
            "#{session_name}", "#{window_index}", "#{window_name}", "#{pane_index}",
            "#{pane_current_command}", "#{pane_title}", "#{pane_current_path}",
            "#{pane_active}", "#{pane_width}", "#{pane_height}", "#{window_activity}",
        ])
        raw = self._run("list-panes", "-a", "-F", format_string)
        panes = []
        for line in raw.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) not in (12, 13):
                continue
            pane_id, target, session, window, window_name, index, command, title, path, active, width, height = fields[:12]
            activity = fields[12] if len(fields) > 12 else ""
            panes.append({
                "id": pane_id,
                "host_id": "localhost",
                "target": target,
                "session": session,
                "window": window,
                "window_name": window_name,
                "index": index,
                "command": command,
                "title": title,
                "path": path,
                "active": active == "1",
                "activity": int(activity) if str(activity).isdigit() else 0,
                "width": int(width),
                "height": int(height),
            })
        return panes

    def validate_pane(self, pane_id: str) -> dict:
        if not PANE_RE.fullmatch(pane_id):
            raise TmuxError("invalid pane id")
        for pane in self.list_panes():
            if pane["id"] == pane_id:
                return pane
        raise TmuxError("pane not found")

    def capture_many(self, pane_ids, history: int = 40) -> dict:
        """Local captures are a few ms each; this keeps the sweep's interface uniform."""
        result = {}
        for pane_id in pane_ids:
            try:
                result[pane_id] = self.capture(pane_id, history)
            except (RuntimeError, OSError, ValueError, TmuxError):
                continue
        return result

    def capture(self, pane_id: str, history: int = 500) -> str:
        self.validate_pane(pane_id)
        history = max(20, min(history, 5000))
        content = self._run("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{history}")
        return ANSI_RE.sub("", content)

    def capture_ansi(self, pane_id: str, history: int = 500) -> str:
        self.validate_pane(pane_id)
        history = max(20, min(history, 5000))
        return self._run("capture-pane", "-p", "-J", "-e", "-t", pane_id, "-S", f"-{history}")

    def pane_pid(self, pane_id: str) -> str:
        return self._run("display-message", "-p", "-t", pane_id, "#{pane_pid}").strip()

    def transcript(self, pane_id: str, limit: int = 200) -> dict:
        # Discovery and parsing live in transcript_reader so the exact same code
        # produces transcripts here and on SSH remote hosts.
        pane = self.validate_pane(pane_id)
        provider = transcript_reader.provider_for_command(pane.get("command"))
        if provider is None:
            raise TmuxError("structured transcript is not supported for this pane")
        # A resumed CLI session tells us exactly which transcript to read; the
        # local/web gateway does not, so the reader falls back to cwd probing.
        hint = transcript_reader.resume_session_hint(self.pane_pid(pane_id))
        try:
            data = transcript_reader.read_transcript(
                provider, pane.get("path", ""), pane.get("title", ""), limit,
                home=Path.home(), hint=hint, screen=lambda: self.capture(pane_id, 80),
            )
        except transcript_reader.TranscriptError as exc:
            raise TmuxError(str(exc)) from exc
        data["pane"] = pane_id
        return data

    def snapshot_ansi(self, pane_id: str, history: int = 5000,
                      known_digest: str | None = None) -> tuple[str, str | None]:
        """Return (digest, content); content is None when nothing changed."""
        content = self.capture_ansi(pane_id, history)
        digest = hashlib.sha1(content.encode()).hexdigest()
        if known_digest is not None and digest == known_digest:
            return digest, None
        return digest, content

    def list_files(self, directory: str, show_hidden: bool = False, limit: int = 2000) -> dict:
        try:
            return file_browser.list_directory(directory, show_hidden=show_hidden, limit=limit)
        except (file_browser.FileBrowserError, OSError, ValueError) as exc:
            raise TmuxError(str(exc)) from exc

    def download_file(self, path: str, max_bytes: int) -> bytes:
        resolved = file_browser.resolve_path(path)
        if not resolved.is_file():
            raise TmuxError("not a file: %s" % resolved)
        if resolved.stat().st_size > max_bytes:
            raise TmuxError("file is larger than the download limit")
        with resolved.open("rb") as handle:
            return handle.read(max_bytes + 1)

    def zip_files(self, paths: list[str], base: str, max_bytes: int, max_files: int) -> bytes:
        try:
            return file_browser.zip_paths(paths, base, max_bytes=max_bytes, max_files=max_files)
        except (file_browser.FileBrowserError, OSError, ValueError) as exc:
            raise TmuxError(str(exc)) from exc

    def upload_file(self, path: str, data: bytes) -> None:
        resolved = file_browser.resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a dropped file never truncates
        # an existing one until the bytes are complete.
        temporary = resolved.with_name(f".{resolved.name}.upload-{secrets.token_hex(4)}")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
            os.replace(temporary, resolved)
        finally:
            if temporary.exists():
                temporary.unlink()

    def send_key(self, pane_id: str, key: str) -> None:
        self.validate_pane(pane_id)
        self._run("send-keys", "-t", pane_id, key)

    def send(self, pane_id: str, text: str, submit: bool = True) -> None:
        pane = self.validate_pane(pane_id)
        buffer_name = f"tmux-web-{secrets.token_hex(8)}"
        try:
            if submit:
                self._run("send-keys", "-t", pane_id, "C-u")
                time.sleep(0.05)
            self._run("load-buffer", "-b", buffer_name, "-", input_bytes=text.encode())
            self._run("paste-buffer", "-b", buffer_name, "-d", "-t", pane_id)
            if submit:
                time.sleep(0.25 if pane.get("command") == "python3" else 0.12)
                self._run("send-keys", "-t", pane_id, "C-m")
        finally:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                capture_output=True,
                check=False,
            )


class InputRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    submit: bool = True


ALLOWED_KEYS = {"Escape", "Enter", "Tab", "C-c", "C-d", "C-q", "C-u", "Up", "Down", "Left", "Right"}

def interrupt_key(provider: str | None) -> str:
    """Which key stops a running turn for this agent CLI.

    Hermes interrupts on Ctrl+Q (Ctrl+C also interrupts but a second press within
    two seconds force-exits the TUI, so it is the wrong thing to send blindly).
    Claude Code, Codex and Kimi cancel on Escape.
    """
    return "C-q" if provider == "hermes" else "Escape"


class KeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=16)


class ShareCreateRequest(BaseModel):
    pane_id: str
    slug: str = Field(min_length=3, max_length=48)
    password: str = Field(default="", max_length=256)
    expires_in: int = Field(ge=300, le=60 * 60 * 24 * 90)
    # read-only by default? no: existing behaviour keeps input enabled unless asked
    allow_input: bool = True


class ShareExtendRequest(BaseModel):
    seconds: int = Field(ge=300, le=60 * 60 * 24 * 90)


class SharePasswordRequest(BaseModel):
    password: str = Field(default="", max_length=256)


class PasswordChangeRequest(BaseModel):
    current: str = Field(default="")
    new: str = Field(default="", alias="next")

    class Config:
        populate_by_name = True


class SessionCreateRequest(BaseModel):
    host: str = Field(default="localhost", max_length=64)
    name: str = Field(min_length=1, max_length=64)
    path: str = Field(default="", max_length=4096)


class ZipRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=1000)
    base: str = Field(default="", max_length=4096)


class RemoteHostCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    auth_type: str
    credential: str = Field(default="", max_length=100_000)
    # empty when the host was saved without a reachable scan: the key is pinned
    # on the first successful connection (trust on first use)
    host_key: str = Field(default="", max_length=20_000)
    fingerprint: str = Field(default="", max_length=256)
    confirm_fingerprint: str = Field(default="", max_length=256)
    # the hop to dial first: either a saved host id, or a "user@host:port" typed in
    # directly (it then borrows this host's credential); at most one of the two
    jump_host_id: str = Field(default="", max_length=64)
    jump_spec: str = Field(default="", max_length=300)
    # saving with a wrong password/key is allowed: the host is stored and shows
    # up as offline until the credential is fixed
    verify: bool = True


class RemoteHostUpdateRequest(BaseModel):
    """Partial edit: omitted fields keep their stored value."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    auth_type: str | None = None
    credential: str | None = Field(default=None, max_length=100_000)
    verify: bool | None = None
    host_key: str | None = Field(default=None, min_length=20, max_length=20_000)
    fingerprint: str | None = Field(default=None, min_length=8, max_length=256)
    jump_host_id: str | None = Field(default=None, max_length=64)
    jump_spec: str | None = Field(default=None, max_length=300)


class RemoteHostReorderRequest(BaseModel):
    ids: list[str] = Field(min_length=0, max_length=500)


class RelayTimeoutError(Exception):
    """A relay question timed out; ``tail`` carries the pane's last lines."""

    def __init__(self, message: str, tail: str = ""):
        super().__init__(message)
        self.tail = tail


class McpToolError(Exception):
    def __init__(self, message: str, tail: str = ""):
        super().__init__(message)
        self.tail = tail


MCP_PROTOCOL_VERSION = "2025-06-18"


class HomeRecentRequest(BaseModel):
    pane: str = ""


class HomeFavoriteRequest(BaseModel):
    pane: str = Field(min_length=1, max_length=400)
    note: str = ""
    title: str = ""


AGENT_PREVIEW_CHARS = 6000

class HomeFavoriteTitleRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class HomeCardRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    pane: str = ""
    kind: str = "note"


class RelayAskRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=100_000)
    timeout: int = Field(default=180, ge=5, le=600)
    force: bool = False
    source: str = Field(default="", max_length=120)


class RelayMessageRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=100_000)


def pane_metadata(pane: dict) -> dict:
    """Annotate a pane with its provider and what TmuxWeb can do with it.

    Clients can then hide actions that would only fail, instead of discovering
    support by triggering a 409.
    """
    provider = transcript_reader.provider_for_command(pane.get("command"))
    pane["provider"] = provider
    pane["capabilities"] = {
        "transcript": bool(provider),
        "terminal": True,
        "relay": bool(provider),
        # any pane can be shared; ones without a transcript fall back to screen text
        "share": True,
    }
    return pane


class TerminalSession:
    def __init__(self, pane_id: str, cols: int, rows: int):
        self.pane_id = pane_id
        # Read the size BEFORE attaching: tmux resizes the window as soon as the client
        # shows up (the pty starts at 0x0), so a later query would record the size we
        # just imposed and "restoring" it would be a no-op.
        self.previous_geometry = self._tmux_value(
            "display", "-p", "-t", pane_id, "#{window_width}x#{window_height}")
        # set-option is a session command: a pane target would not resolve, and the
        # restored size would fall back to the global 80x24
        self.session_name = self._tmux_value("display", "-p", "-t", pane_id, "#{session_name}")
        # how this window decides its size: "latest"/"largest"/... or manual. Restoring
        # the size leaves the window on `manual` (resize-window forces it), which stops it
        # following clients, so the mode has to come back too.
        own_mode = self._tmux_value("show-options", "-w", "-v", "-t", pane_id, "window-size")
        self.window_size_mode = own_mode or self._tmux_value(
            "show-options", "-g", "-v", "window-size") or "latest"
        self.master_fd, slave_fd = os.openpty()
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        self.process = subprocess.Popen(
            ["tmux", "attach-session", "-t", pane_id],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=environment,
            close_fds=True,
        )
        os.close(slave_fd)
        self._closed = False
        self._set_size(cols, rows)

    @staticmethod
    def _tmux_value(*args: str) -> str:
        """Best-effort tmux query: a terminal must still open when this fails."""
        try:
            result = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip()

    def restore_geometry(self) -> None:
        """Put the window size back after the last client leaves.

        tmux derives a window's size from the clients still attached; when the phone is
        the last one and detaches, the window keeps the 47x40 it was handed, so the pane
        stays tiny for the next viewer. Restoring needs three steps: `resize-window`
        sets the size but pins `window-size` to manual (the session then stops following
        its clients), and unsetting it alone falls back to `default-size` (80x24), so the
        session's default-size is temporarily pointed at the size we are restoring.
        """
        geometry = getattr(self, "previous_geometry", "")
        if "x" not in geometry:
            return
        cols, _, rows = geometry.partition("x")
        if not (cols.isdigit() and rows.isdigit()):
            return
        if self._tmux_value("list-clients", "-t", self.pane_id, "-F", "."):
            return                      # someone is watching: their size wins
        session = getattr(self, "session_name", "") or self.pane_id
        # Only this window is touched: other windows of the same session and other
        # sessions keep their own size, which is what a viewer expects.
        self._tmux_value("resize-window", "-t", self.pane_id, "-x", cols, "-y", rows)
        mode = getattr(self, "window_size_mode", "") or "latest"
        self._tmux_value("set-option", "-w", "-t", self.pane_id, "window-size", mode)

    def _set_size(self, cols: int, rows: int) -> None:
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 300))
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        # The child inherits the pty instead of opening it (start_new_session + inherited
        # fds), so it never becomes this tty's controlling terminal and the kernel does not
        # deliver SIGWINCH. Without this signal tmux keeps its old size and never repaints:
        # resizing a phone terminal did nothing at all.
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            try:
                os.kill(process.pid, signal.SIGWINCH)
            except (ProcessLookupError, PermissionError):
                pass

    async def read(self) -> bytes:
        try:
            return await asyncio.to_thread(os.read, self.master_fd, 65536)
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:
        if data:
            await asyncio.to_thread(os.write, self.master_fd, data)

    async def resize(self, cols: int, rows: int) -> None:
        await asyncio.to_thread(self._set_size, cols, rows)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        await asyncio.to_thread(self.restore_geometry)


def session_token(password: str, secret: str) -> str:
    return hmac.new(secret.encode(), password.encode(), hashlib.sha256).hexdigest()


def hash_share_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_share_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


logger = logging.getLogger("tmux_web")


SHARE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS shares (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        pane_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        title TEXT NOT NULL,
        password_hash TEXT,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        revoked_at REAL,
        allow_input INTEGER NOT NULL DEFAULT 1
    )
"""


def ensure_share_schema(connection) -> None:
    """Create the shares table and add columns that newer versions need."""
    with connection:
        connection.execute(SHARE_SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(shares)")}
        if "allow_input" not in columns:  # databases from before read-only shares
            connection.execute("ALTER TABLE shares ADD COLUMN allow_input INTEGER NOT NULL DEFAULT 1")


def create_app(
    backend: TmuxBackend | None = None,
    *,
    password: str | None = None,
    session_secret: str | None = None,
    terminal_factory=None,
    share_db: str | None = None,
    home_db: str | None = None,
    remote_store=None,
    remote_runner_factory=None,
) -> FastAPI:
    tmux = backend or TmuxBackend()
    configured_password = password if password is not None else os.environ.get("TMUX_WEB_PASSWORD")
    if not configured_password:
        raise RuntimeError("TMUX_WEB_PASSWORD must be set")
    secret = session_secret or os.environ.get("TMUX_WEB_SESSION_SECRET") or secrets.token_hex(32)
    # The password can be changed from the console's settings panel; the store keeps a hash and,
    # once it exists, session tokens derive from that hash instead of the environment value.
    password_store = PasswordStore(
        os.environ.get("TMUX_WEB_PASSWORD_DB") or str(BASE_DIR / "passwords.json")
    )

    def check_password(candidate: str) -> bool:
        if password_store.encoded():
            return password_store.verify(candidate)
        return hmac.compare_digest(candidate, configured_password)

    def token_seed() -> str:
        return password_store.material() or configured_password

    def expected_token() -> str:
        return session_token(token_seed(), secret)
    api_token = (os.environ.get("TMUX_WEB_API_TOKEN") or "").strip()
    # File transfers are buffered in memory, so both directions are capped.
    download_limit = int(os.environ.get("TMUX_WEB_FILE_DOWNLOAD_MAX", str(64 * 1024 * 1024)))
    zip_limit = int(os.environ.get("TMUX_WEB_ZIP_MAX", str(file_browser.DEFAULT_ZIP_BYTES)))
    zip_max_files = int(os.environ.get("TMUX_WEB_ZIP_MAX_FILES", str(file_browser.DEFAULT_ZIP_FILES)))
    upload_limit = int(os.environ.get("TMUX_WEB_FILE_UPLOAD_MAX", str(256 * 1024 * 1024)))
    # The screen stream is a firehose for log panes (uvicorn, tail -f, make). Cap
    # how much history it transfers and how often, and trim oversized frames.
    stream_lines = int(os.environ.get("TMUX_WEB_STREAM_LINES", "800"))
    stream_first_lines = max(stream_lines, int(os.environ.get("TMUX_WEB_STREAM_FIRST_LINES", "1500")))
    stream_max_bytes = int(os.environ.get("TMUX_WEB_STREAM_MAX_BYTES", str(192 * 1024)))
    if importlib.util.find_spec("websockets") is None and importlib.util.find_spec("wsproto") is None:
        LOGGER.warning(
            "No WebSocket implementation installed: uvicorn rejects /ws/* with 404. "
            "Install the 'websockets' package (see requirements.txt)."
        )
    make_terminal = terminal_factory or TerminalSession
    jump_max_depth = max(1, int(os.environ.get("TMUX_WEB_JUMP_MAX_DEPTH", "5")))
    remote_hosts = remote_store or RemoteHostStore(
        os.environ.get("TMUX_WEB_HOST_DB", str(BASE_DIR / "remote_hosts.db")),
        os.environ.get("TMUX_WEB_HOST_KEY", str(BASE_DIR / ".remote_hosts.key")),
    )
    make_remote_runner = remote_runner_factory or SSHRunner

    def runner_for(host: dict):
        """Build a runner; a host saved without a pinned key gets pinned on first use."""
        host = dict(host)
        chain = remote_hosts.jump_chain(host, limit=jump_max_depth)
        if chain:
            host["jump_chain"] = chain
            host["jump"] = chain[0]
        runner = make_remote_runner(host)
        if hasattr(runner, "on_tmux_bin"):
            def remember_tmux(path: str) -> None:
                try:
                    remote_hosts.update_host(host["id"], tmux_path=path)
                except (KeyError, ValueError, OSError):
                    LOGGER.warning("could not persist tmux path for %s", host.get("name") or host.get("id"))
            runner.on_tmux_bin = remember_tmux
        if not (host.get("host_key") or "").strip() and hasattr(runner, "on_host_key"):
            def persist(line: str) -> None:
                try:
                    remote_hosts.update_host(host["id"], host_key=line,
                                             fingerprint=host_key_fingerprint(line))
                except (KeyError, ValueError, OSError):
                    LOGGER.warning("could not pin the host key of %s", host.get("name"), exc_info=True)
            runner.on_host_key = persist
        return runner
    share_db_path = share_db or os.environ.get("TMUX_WEB_SHARE_DB") or str(BASE_DIR / "shares.db")
    share_connection = sqlite3.connect(share_db_path, check_same_thread=False)
    share_connection.row_factory = sqlite3.Row
    share_lock = threading.Lock()
    login_limiter = LoginRateLimiter()
    ensure_share_schema(share_connection)
    home = HomeStore(home_db or os.environ.get("TMUX_WEB_HOME_DB") or str(BASE_DIR / "home.db"))

    mcp_auth_required = os.environ.get("TMUX_WEB_MCP_AUTH", "on").strip().lower() not in {
        "0", "off", "no", "false", "none",
    }
    if not mcp_auth_required:
        logger.warning("MCP endpoint /mcp is unauthenticated (TMUX_WEB_MCP_AUTH=off): "
                       "anyone who can reach this port can drive agent panes")

    app = FastAPI(title="Tmux Web Console", docs_url=None, redoc_url=None, openapi_url=None)

    allowed_hosts = {item.strip().lower() for item in
                     os.environ.get("TMUX_WEB_ALLOWED_HOSTS", "").split(",") if item.strip()}
    if allowed_hosts:
        @app.middleware("http")
        async def enforce_allowed_hosts(request: Request, call_next):
            host = (request.headers.get("host") or "").split(":")[0].lower()
            if host and host not in allowed_hosts and not host.startswith(("127.0.0.1", "localhost")):
                return JSONResponse({"detail": "host not allowed"}, status_code=421)
            return await call_next(request)

    trust_proxy = os.environ.get("TMUX_WEB_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}

    def client_address(request: Request) -> str:
        """Real client IP; behind Cloudflare/nginx the socket peer is the proxy."""
        if trust_proxy:
            forwarded = request.headers.get("cf-connecting-ip") or ""
            if not forwarded:
                forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            if forwarded:
                return forwarded
        return request.client.host if request.client else "unknown"

    def login_key(request: Request, scope: str) -> str:
        # rate limit per real client, not per proxy address
        return f"{scope}:{client_address(request)}"

    def secure_cookie(request: Request) -> bool:
        setting = os.environ.get("TMUX_WEB_SECURE_COOKIES", "auto").strip().lower()
        if setting in {"1", "true", "yes", "on"}:
            return True
        if setting in {"0", "false", "no", "off"}:
            return False
        if trust_proxy and (request.headers.get("x-forwarded-proto") or "").lower() == "https":
            return True
        return request.url.scheme == "https"

    def authenticated(request: Request) -> bool:
        token = request.cookies.get(COOKIE_NAME, "")
        if token and hmac.compare_digest(token, expected_token()):
            return True
        if not api_token:
            return False
        # Programmatic access (relay CLI, other agents on other servers) uses a
        # long-lived token instead of the interactive login cookie.
        supplied = request.headers.get("x-tmuxweb-token", "").strip()
        if not supplied:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                supplied = header[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, api_token)

    def require_auth(request: Request) -> None:
        if not authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")

    def pane_or_404(pane_id: str) -> dict:
        try:
            host_id, actual_pane_id = split_pane_ref(pane_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="invalid pane reference") from exc
        if host_id != "localhost":
            try:
                host = remote_hosts.get_host(host_id, include_credential=True)
                panes = runner_for(host).list_panes()
            except (KeyError, RuntimeError, OSError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            for pane in panes:
                if pane.get("pane_id") == actual_pane_id:
                    return pane
            raise HTTPException(status_code=404, detail="remote pane not found")
        try:
            return tmux.validate_pane(pane_id)
        except TmuxError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def pane_transport(pane_ref: str):
        try:
            host_id, pane_id = split_pane_ref(pane_ref)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="invalid pane reference") from exc
        if host_id == "localhost":
            return tmux, pane_id
        try:
            host = remote_hosts.get_host(host_id, include_credential=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="remote host not found") from exc
        return runner_for(host), pane_id

    def relay_target(target: str):
        """Resolve a relay target to (transport, pane_id, pane, host_name)."""
        try:
            host_id, pane_id = split_pane_ref(target)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="invalid pane reference") from exc
        if host_id == "localhost":
            try:
                return tmux, pane_id, tmux.validate_pane(pane_id), "localhost"
            except TmuxError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            host = remote_hosts.get_host(host_id, include_credential=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="remote host not found") from exc
        runner = runner_for(host)
        try:
            panes = runner.list_panes()
        except (RuntimeError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        for pane in panes:
            if pane.get("pane_id") == pane_id:
                return runner, pane_id, pane, host.get("name") or host_id
        raise HTTPException(status_code=404, detail="remote pane not found")

    def relay_hosts() -> list[tuple[str, str, object, list[dict]]]:
        """Every reachable transport plus its panes, for relay/status discovery."""
        found: list[tuple[str, str, object, list[dict]]] = []
        try:
            found.append(("localhost", "localhost", tmux, tmux.list_panes()))
        except TmuxError:
            pass
        for remote in remote_hosts.list_hosts():
            try:
                credentialed = remote_hosts.get_host(remote["id"], include_credential=True)
                runner = runner_for(credentialed)
                found.append((remote["id"], remote.get("name") or remote["id"], runner, runner.list_panes()))
            except (KeyError, RuntimeError, OSError):
                continue
        return found

    def collect_pane_states() -> list[dict]:
        # Reuse the pane list /api/hosts just probed (the console and the board poll
        # it) instead of listing every host's panes again: the sweep only needs the
        # screens, one batched capture per host.
        jobs = []
        snapshot = hosts_snapshot["value"]
        if snapshot and (time.time() - hosts_snapshot["at"]) < 15:
            for host in snapshot.get("hosts") or []:
                host_id = str(host.get("id") or "")
                panes = host.get("panes") or []
                if not panes:
                    continue
                if host_id == "localhost":
                    transport = tmux
                else:
                    try:
                        transport = runner_for(remote_hosts.get_host(host_id, include_credential=True))
                    except (KeyError, RuntimeError, OSError, ValueError):
                        continue
                for pane in panes:
                    jobs.append((host_id, host.get("name") or host_id, transport, pane))
        else:
            for host_id, host_name, transport, panes in relay_hosts():
                for pane in panes:
                    jobs.append((host_id, host_name, transport, pane))
        if not jobs:
            return []

        sweep_timeout = int(os.environ.get("TMUX_WEB_STATE_CAPTURE_TIMEOUT", "8"))
        for _, _, transport, _pane in jobs:
            if hasattr(transport, "capture_timeout"):
                transport.capture_timeout = sweep_timeout

        # One capture per host: the tail text is classified locally afterwards.
        captured: dict[int, dict] = {}
        def grab(transport_panes):
            key, transport, pane_ids = transport_panes
            try:
                return key, transport.capture_many(pane_ids, 40)
            except Exception:  # noqa: BLE001 - fall back to per-pane captures
                return key, {}
        groups: dict[int, tuple] = {}
        for job in jobs:
            transport = job[2]
            actual = job[3].get("pane_id") or job[3]["id"]
            entry = groups.setdefault(id(transport), (id(transport), transport, []))
            entry[2].append(actual)
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(groups) or 1))) as pool:
            for index, texts in pool.map(grab, list(groups.values())):
                captured[index] = texts

        # Ask each agent's own session file whether its turn is running: the screen lies
        # (a status bar saying "thinking: high" is a model setting, a TUI redraws while
        # idle) while the session file records the turn boundary. Remote hosts answer for
        # all their panes in one ssh call.
        native: dict[str, str] = {}
        local_agents = [job[3] for job in jobs
                        if job[0] == "localhost"
                        and transcript_reader.provider_for_command(job[3].get("command"))]
        remote_agents: dict[int, tuple] = {}
        for job in jobs:
            host_id, _, transport, pane = job
            provider = transcript_reader.provider_for_command(pane.get("command"))
            if host_id == "localhost" or not provider or not hasattr(transport, "status_many"):
                continue
            group = remote_agents.setdefault(id(transport), (transport, host_id, []))
            group[2].append(pane.get("pane_id") or pane["id"])

        def ask_host(group):
            transport, host_id, ids = group
            try:
                states = transport.status_many(ids) or {}
            except Exception:  # noqa: BLE001 - a dead host keeps its screen verdict
                return []
            # host-scoped: "%8" exists on several hosts and means a different pane
            return [(f"{host_id}~{pane_id}" if "~" not in str(pane_id) else str(pane_id), state)
                    for pane_id, state in states.items()]

        def probe_native():
            groups = list(remote_agents.values())
            if not groups:
                return []
            answers = []
            with ThreadPoolExecutor(max_workers=max(1, min(6, len(groups)))) as host_pool:
                for listing in host_pool.map(ask_host, groups):
                    answers.extend(listing)
            return answers

        if local_agents:
            def probe_local():
                out = []
                for pane in local_agents:
                    provider = transcript_reader.provider_for_command(pane.get("command"))
                    try:
                        state = agent_status.status_for_pane(
                            provider, pane.get("path", ""), pane.get("title", ""))
                    except Exception:  # noqa: BLE001
                        continue
                    if state in (agent_status.BUSY, agent_status.IDLE):
                        out.append((pane["id"], state))
                return out
        else:
            def probe_local():
                return []

        with ThreadPoolExecutor(max_workers=2) as probe_pool:
            local_future = probe_pool.submit(probe_local)
            remote_future = probe_pool.submit(probe_native)
            for listing in (local_future.result(), remote_future.result()):
                for key, state in listing:
                    native[key] = state

        busy_window = float(os.environ.get("TMUX_WEB_ACTIVITY_BUSY_SECONDS", "3"))
        now = time.time()

        def describe(job):
            host_id, host_name, transport, pane = job
            provider = transcript_reader.provider_for_command(pane.get("command"))
            actual_pane_id = pane.get("pane_id") or pane["id"]
            text = captured.get(id(transport), {}).get(actual_pane_id)
            activity = pane.get("activity") or 0
            age = (now - activity) if activity else None
            surface = native.get(pane["id"])
            if surface in (agent_status.BUSY, agent_status.IDLE):
                status, source = surface, "session"
            elif age is not None and age <= busy_window:
                status, source = "busy", "activity"
            elif text is not None:
                try:
                    status, source = relay.classify(text), "screen"
                except Exception:  # noqa: BLE001
                    status, source = "unknown", "screen"
            else:
                try:
                    state = relay.inspect(transport, actual_pane_id, provider, lines=40)
                    status, source = state["status"], "screen"
                except Exception:  # noqa: BLE001 - one slow or dead pane must not fail the sweep
                    status, source = "unknown", "screen"
            target = pane.get("target") or ""
            return {"id": pane["id"], "pane_id": actual_pane_id, "host_id": host_id,
                    "host_name": host_name, "target": target, "provider": provider,
                    "status": status, "status_source": source, "activity_age": age,
                    "title": pane.get("title", ""), "path": pane.get("path", ""),
                    "session": target.split(":")[0] if target else "",
                    "window": pane.get("window_name") or target}

        workers = max(4, min(32, int(os.environ.get("TMUX_WEB_STATE_WORKERS", "24"))))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(describe, jobs))

    # ``collect_pane_states`` inspects every pane on every host; the console polls
    # it every 9s and the Home board needs it for its counters. Cache it briefly and
    # refresh in the background so a page load never pays for the whole sweep.
    pane_state_cache: dict = {"at": 0.0, "states": []}
    pane_state_lock = threading.Lock()
    pane_state_busy = {"flag": False}

    def _refresh_pane_states() -> None:
        started_at = time.time()
        try:
            states = collect_pane_states()
            with pane_state_lock:
                pane_state_cache.update({"at": time.time(), "states": states})
            elapsed = time.time() - started_at
            log = LOGGER.warning if elapsed > 3 else LOGGER.info
            log("pane state sweep: %d panes in %.1fs", len(states), elapsed)
        except Exception:  # noqa: BLE001 - a failed sweep must never kill the poller
            LOGGER.warning("pane state sweep failed after %.1fs", time.time() - started_at, exc_info=True)
        finally:
            pane_state_busy["flag"] = False

    def pane_states_cached(wait: float = 3.0, ttl: float = 30.0) -> list[dict]:
        """Last known pane states, refreshed once they are older than ``ttl``.

        ``wait`` bounds how long a caller blocks for a fresh sweep; a warm cache
        returns at once and a cold one returns whatever is already there after
        the deadline, while the sweep keeps running in the background.
        """
        with pane_state_lock:
            fresh = (time.time() - pane_state_cache["at"]) < ttl
            states = pane_state_cache["states"]
        if fresh:
            return states
        started = threading.Event()

        def refresh() -> None:
            _refresh_pane_states()
            started.set()

        with pane_state_lock:
            should_start = not pane_state_busy["flag"]
            if should_start:
                pane_state_busy["flag"] = True
        if should_start:
            threading.Thread(target=refresh, daemon=True).start()
        started.wait(max(0.0, wait))
        with pane_state_lock:
            return pane_state_cache["states"]

    hosts_snapshot: dict = {"at": 0.0, "value": None,
                            "ttl": float(os.environ.get("TMUX_WEB_HOSTS_TTL", "5"))}
    relay_log: deque[dict] = deque(maxlen=100)
    relay_locks: dict[str, asyncio.Lock] = {}

    def get_share(slug: str):
        with share_lock:
            row = share_connection.execute("SELECT * FROM shares WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="share not found")
        if row["revoked_at"] is not None or row["expires_at"] <= time.time():
            raise HTTPException(status_code=410, detail="share expired or revoked")
        return row

    def share_cookie_name(slug: str) -> str:
        return "tmux_share_" + hashlib.sha256(slug.encode()).hexdigest()[:16]

    def share_access_token(row) -> str:
        value = f"{row['id']}:{row['expires_at']}:{row['password_hash'] or ''}"
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def require_share_access(request: Request, row) -> None:
        if not row["password_hash"]:
            return
        token = request.cookies.get(share_cookie_name(row["slug"]), "")
        if not hmac.compare_digest(token, share_access_token(row)):
            raise HTTPException(status_code=401, detail="share password required")

    def validate_shared_session(row) -> dict:
        if not row["session_id"]:
            # screen shares follow the pane itself, not a provider session
            return pane_or_404(row["pane_id"])
        try:
            transport, actual_pane_id = pane_transport(row["pane_id"])
            pane = pane_or_404(row["pane_id"])
            data = transport.transcript(actual_pane_id, 1)
        except (HTTPException, TmuxError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="shared session is unavailable") from exc
        if str(data.get("session_id")) != row["session_id"]:
            raise HTTPException(status_code=409, detail="shared pane no longer points to the original session")
        return pane

    def login_redirect(request: Request) -> RedirectResponse:
        """Send an anonymous visitor to /login and come back to exactly this page."""
        target = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)

    @app.get("/preview", response_class=HTMLResponse)
    async def preview_page(request: Request):
        """Full chat view for one pane; /home opens it in a drawer (desktop) or directly (mobile)."""
        if not authenticated(request):
            return login_redirect(request)
        return FileResponse(WEB_DIR / "preview.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/home", response_class=HTMLResponse)
    async def home_page(request: Request):
        if not authenticated(request):
            return login_redirect(request)
        return FileResponse(WEB_DIR / "home.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/term", response_class=HTMLResponse)
    async def term_page(request: Request):
        """One bare terminal (xterm + PTY), used as a /grid cell and on its own."""
        if not authenticated(request):
            return login_redirect(request)
        return FileResponse(WEB_DIR / "term.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/grid", response_class=HTMLResponse)
    async def grid_page(request: Request):
        """Several conversations side by side; each cell is an embedded preview."""
        if not authenticated(request):
            return login_redirect(request)
        return FileResponse(WEB_DIR / "grid.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    def login_markup(target: str, error: str = "") -> str:
        markup = (WEB_DIR / "login.html").read_text()
        markup = markup.replace("<!--NEXT-->", f'<input type="hidden" name="next" value="{escape(target, quote=True)}">')
        return markup.replace("<!--ERROR-->", f'<p class="error" role="alert">{error}</p>' if error else "")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        target = safe_next_url(request.query_params.get("next", "/"))
        if authenticated(request):
            return RedirectResponse(target, status_code=303)
        return HTMLResponse(login_markup(target), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login(request: Request):
        rate_key = login_key(request, "admin")
        login_limiter.check(rate_key)
        body = (await request.body()).decode(errors="replace")
        fields = parse_qs(body)
        supplied = fields.get("password", [""])[0]
        target = safe_next_url(fields.get("next", ["/"])[0])
        if not check_password(supplied):
            login_limiter.fail(rate_key)
            return HTMLResponse(
                login_markup(target, error="密码错误"), status_code=401
            )
        login_limiter.success(rate_key)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            expected_token(),
            httponly=True,
            samesite="strict",
            secure=secure_cookie(request),
            max_age=60 * 60 * 24 * 7,
            path="/",
        )
        return response

    @app.get("/api/settings")
    async def settings_info(request: Request):
        require_auth(request)
        return {
            "password_source": "file" if password_store.encoded() else "env",
            "hosts": len(remote_hosts.list_hosts()),
            "api_token": bool(api_token),
        }

    @app.get("/api/settings/export")
    async def export_settings(request: Request):
        """Everything needed to rebuild this console on another machine, credentials included.

        The browser encrypts this before it reaches disk, so the password never reaches us; the
        response is marked no-store so a shared machine does not keep plaintext secrets in cache.
        """
        require_auth(request)
        fields = ("name", "hostname", "port", "username", "auth_type", "credential", "host_key",
                  "fingerprint", "jump_host_id", "jump_spec", "tmux_path", "position", "enabled")
        hosts = []
        names = {}
        for host in remote_hosts.list_hosts():
            full = remote_hosts.get_host(host["id"], include_credential=True) or {}
            names[host["id"]] = full.get("name") or host["id"]
            hosts.append({key: full.get(key, "") for key in fields})
        for entry in hosts:
            # ids are local, names are not: an import relinks the hop by name
            entry["jump_name"] = names.get(entry.get("jump_host_id") or "", "")
        board = {}
        try:
            board = {"favorites": home.favorites()} if hasattr(home, "favorites") else {}
        except Exception:  # a missing board must not block an export
            board = {}
        return JSONResponse(
            {"app": "TmuxWeb", "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "hosts": hosts,
             "board": board},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/settings/password")
    async def change_password(request: Request, payload: PasswordChangeRequest):
        """Rotate the console password. Sessions minted from the old one stop working."""
        require_auth(request)
        current = payload.current or ""
        wanted = (payload.new or "").strip()
        if not check_password(current):
            raise HTTPException(status_code=403, detail="当前密码不正确")
        if len(wanted) < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 位")
        if wanted == current:
            raise HTTPException(status_code=400, detail="新密码不能和当前密码相同")
        password_store.set(wanted)
        response = JSONResponse({"ok": True, "password_source": "file"})
        # keep this browser signed in; other devices have to log in again
        response.set_cookie(
            COOKIE_NAME,
            expected_token(),
            httponly=True,
            samesite="strict",
            secure=secure_cookie(request),
            max_age=60 * 60 * 24 * 7,
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/")
    async def index(request: Request):
        if not authenticated(request):
            return RedirectResponse(f"/login?next={quote(request.url.path + ('?' + request.url.query if request.url.query else ''))}", status_code=303)
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/shares")
    async def shares_page(request: Request):
        if not authenticated(request):
            return login_redirect(request)
        return FileResponse(WEB_DIR / "shares.html", headers={"Cache-Control": "no-store"})

    @app.get("/hosts")
    async def hosts_page(request: Request):
        if not authenticated(request):
            return login_redirect(request)
        return FileResponse(WEB_DIR / "hosts.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/shares", status_code=201)
    async def create_share(request: Request, payload: ShareCreateRequest):
        require_auth(request)
        if not SHARE_SLUG_RE.fullmatch(payload.slug):
            raise HTTPException(status_code=422, detail="slug must use lowercase letters, numbers, and hyphens")
        pane = pane_or_404(payload.pane_id)
        transport, actual_pane_id = pane_transport(payload.pane_id)
        try:
            transcript_data = await asyncio.to_thread(transport.transcript, actual_pane_id, 1)
        except (TmuxError, RuntimeError, ValueError):
            # a plain shell pane has no transcript: the share shows the screen instead
            transcript_data = {}
        now = time.time()
        share_id = secrets.token_hex(16)
        password_hash = hash_share_password(payload.password) if payload.password else None
        try:
            with share_lock, share_connection:
                share_connection.execute(
                    "INSERT INTO shares(id, slug, pane_id, provider, session_id, title, password_hash,"
                    " created_at, expires_at, revoked_at, allow_input) VALUES (?,?,?,?,?,?,?,?,?,NULL,?)",
                    (share_id, payload.slug, payload.pane_id, transcript_data.get("provider") or "",
                     str(transcript_data.get("session_id") or ""), pane.get("title") or pane.get("target") or "tmux",
                     password_hash, now, now + payload.expires_in, 1 if payload.allow_input else 0),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="share slug already exists") from exc
        return {"id": share_id, "slug": payload.slug, "url": f"/share/{payload.slug}",
                "expires_at": now + payload.expires_in, "password_protected": bool(password_hash),
                "allow_input": bool(payload.allow_input)}

    @app.get("/api/shares")
    async def list_shares(request: Request):
        require_auth(request)
        with share_lock:
            rows = share_connection.execute(
                "SELECT id,slug,pane_id,provider,session_id,title,created_at,expires_at,revoked_at,"
                "password_hash,allow_input FROM shares ORDER BY created_at DESC"
            ).fetchall()
        now = time.time()
        return {"shares": [{
            "id": row["id"], "slug": row["slug"], "url": f"/share/{row['slug']}",
            "pane_id": row["pane_id"], "provider": row["provider"], "session_id": row["session_id"],
            "title": row["title"], "created_at": row["created_at"], "expires_at": row["expires_at"],
            "revoked": row["revoked_at"] is not None, "password_protected": bool(row["password_hash"]),
            "allow_input": bool(row["allow_input"]),
            "status": "stopped" if row["revoked_at"] is not None else ("expired" if row["expires_at"] <= now else "active"),
        } for row in rows]}

    @app.post("/api/shares/{share_id}/extend")
    async def extend_share(request: Request, share_id: str, payload: ShareExtendRequest):
        require_auth(request)
        with share_lock, share_connection:
            row = share_connection.execute("SELECT expires_at,revoked_at FROM shares WHERE id = ?", (share_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="share not found")
            if row["revoked_at"] is not None:
                raise HTTPException(status_code=409, detail="stopped share cannot be extended")
            expires_at = max(time.time(), row["expires_at"]) + payload.seconds
            share_connection.execute("UPDATE shares SET expires_at = ? WHERE id = ?", (expires_at, share_id))
        return {"status": "extended", "id": share_id, "expires_at": expires_at}

    @app.put("/api/shares/{share_id}/password")
    async def reset_share_password(request: Request, share_id: str, payload: SharePasswordRequest):
        require_auth(request)
        password_hash = hash_share_password(payload.password) if payload.password else None
        with share_lock, share_connection:
            cursor = share_connection.execute("UPDATE shares SET password_hash = ? WHERE id = ?", (password_hash, share_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="share not found")
        return {"status": "password_reset", "id": share_id, "password_protected": bool(password_hash)}

    @app.post("/api/shares/{share_id}/stop")
    async def stop_share(request: Request, share_id: str):
        require_auth(request)
        with share_lock, share_connection:
            cursor = share_connection.execute(
                "UPDATE shares SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), share_id)
            )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="active share not found")
        return {"status": "stopped", "id": share_id}

    @app.delete("/api/shares/{share_id}")
    async def delete_share(request: Request, share_id: str):
        require_auth(request)
        with share_lock, share_connection:
            cursor = share_connection.execute("DELETE FROM shares WHERE id = ?", (share_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="share not found")
        return {"status": "deleted", "id": share_id}

    @app.get("/share/{slug}")
    async def share_page(request: Request, slug: str):
        row = get_share(slug)
        unlocked = not row["password_hash"] or hmac.compare_digest(
            request.cookies.get(share_cookie_name(slug), ""), share_access_token(row)
        )
        html = (WEB_DIR / "share.html").read_text().replace("__SHARE_SLUG__", json.dumps(slug)).replace(
            "__SHARE_TITLE__", str(row["title"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ).replace("__SHARE_UNLOCKED__", "true" if unlocked else "false").replace(
            "__SHARE_ALLOW_INPUT__", "true" if row["allow_input"] else "false")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/share-assets/marked.esm.js")
    async def share_marked_asset():
        return FileResponse(
            WEB_DIR / "vendor" / "marked" / "marked.esm.js",
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/share/{slug}/login")
    async def share_login(request: Request, slug: str):
        row = get_share(slug)
        if not row["password_hash"]:
            return RedirectResponse(f"/share/{slug}", status_code=303)
        rate_key = login_key(request, f"share:{slug}")
        login_limiter.check(rate_key)
        body = (await request.body()).decode(errors="replace")
        supplied = parse_qs(body).get("password", [""])[0]
        if not verify_share_password(supplied, row["password_hash"]):
            login_limiter.fail(rate_key)
            return JSONResponse({"detail": "wrong share password"}, status_code=401)
        login_limiter.success(rate_key)
        response = RedirectResponse(f"/share/{slug}", status_code=303)
        response.set_cookie(share_cookie_name(slug), share_access_token(row), httponly=True,
                            samesite="strict", secure=secure_cookie(request), max_age=max(1, int(row["expires_at"] - time.time())),
                            path=f"/share/{slug}")
        return response

    @app.get("/share/{slug}/api/meta")
    async def share_meta(request: Request, slug: str):
        """What a native client needs before opening a share."""
        row = get_share(slug)
        require_share_access(request, row)
        return {
            "slug": row["slug"],
            "title": row["title"],
            "provider": row["provider"] or None,
            "mode": "transcript" if row["session_id"] else "screen",
            "allow_input": bool(row["allow_input"]),
            "password_protected": bool(row["password_hash"]),
            "expires_at": row["expires_at"],
        }

    @app.get("/share/{slug}/api/transcript")
    async def share_transcript(request: Request, slug: str, limit: int = 300):
        row = get_share(slug)
        require_share_access(request, row)
        validate_shared_session(row)
        transport, actual_pane_id = pane_transport(row["pane_id"])
        if not row["session_id"]:
            content = await asyncio.to_thread(transport.capture, actual_pane_id, 2000)
            digest = hashlib.sha1(content.encode()).hexdigest()
            return {"mode": "screen", "provider": row["provider"] or None, "session_id": None,
                    "mtime": digest, "messages": [], "content": content}
        data = await asyncio.to_thread(transport.transcript, actual_pane_id, limit)
        data["mode"] = "transcript"
        return data

    @app.post("/share/{slug}/api/input")
    async def share_input(request: Request, slug: str, payload: InputRequest):
        row = get_share(slug)
        require_share_access(request, row)
        if not row["allow_input"]:
            raise HTTPException(status_code=403, detail="this share is read-only")
        validate_shared_session(row)
        transport, actual_pane_id = pane_transport(row["pane_id"])
        await asyncio.to_thread(transport.send, actual_pane_id, payload.text, payload.submit)
        return {"status": "sent", "submit": payload.submit}

    @app.get("/manifest.webmanifest")
    async def web_manifest() -> FileResponse:
        """Public on purpose: a browser installs the PWA before anyone logs in."""
        return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="text/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/assets/shell.{kind}")
    async def shell_asset(kind: str):
        """The session sidebar shared by /home and /grid."""
        if kind not in {"css", "js"}:
            raise HTTPException(status_code=404, detail="asset not found")
        media = "text/css" if kind == "css" else "text/javascript"
        return FileResponse(WEB_DIR / "assets" / f"shell.{kind}", media_type=media,
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/app-icon.svg")
    async def app_icon_svg() -> FileResponse:
        return FileResponse(WEB_DIR / "app-icon.svg", media_type="image/svg+xml")

    @app.get("/assets/app-icon-maskable-{size}.png")
    async def app_icon_maskable_png(size: int):
        if size not in {192, 512}:
            raise HTTPException(status_code=404, detail="icon not found")
        return FileResponse(WEB_DIR / f"app-icon-maskable-{size}.png", media_type="image/png")

    @app.get("/assets/app-icon-{size}.png")
    async def app_icon_png(size: int):
        if size not in {32, 180, 192, 512}:
            raise HTTPException(status_code=404, detail="icon not found")
        return FileResponse(WEB_DIR / f"app-icon-{size}.png", media_type="image/png")

    @app.get("/assets/xterm/{filename}")
    async def xterm_asset(request: Request, filename: str):
        require_auth(request)
        if filename not in {"xterm.css", "xterm.js", "addon-fit.js"}:
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(WEB_DIR / "vendor" / "xterm" / filename)

    @app.get("/assets/mermaid/{filename}")
    async def mermaid_asset(request: Request, filename: str):
        require_auth(request)
        if filename != "mermaid.min.js":
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(WEB_DIR / "vendor" / "mermaid" / filename,
                            media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/assets/ansi_up.js")
    async def ansi_up_asset(request: Request):
        require_auth(request)
        return FileResponse(WEB_DIR / "vendor" / "ansi_up" / "ansi_up.js", media_type="text/javascript")

    @app.get("/assets/marked.esm.js")
    async def marked_asset(request: Request):
        require_auth(request)
        return FileResponse(WEB_DIR / "vendor" / "marked" / "marked.esm.js", media_type="text/javascript")

    @app.get("/api/panes")
    async def list_panes(request: Request):
        require_auth(request)
        try:
            return {"panes": [pane_metadata(pane) for pane in tmux.list_panes()]}
        except TmuxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def refresh_one_host(host_id: str, payload: dict) -> dict | None:
        """Re-list the panes of a single host; None when it is not in the snapshot."""
        fresh = copy.deepcopy(payload)
        target = next((item for item in fresh.get("hosts") or [] if item.get("id") == host_id), None)
        if target is None:
            return None
        if host_id == "localhost":
            try:
                panes = [pane_metadata(pane) for pane in await asyncio.to_thread(tmux.list_panes)]
                for pane in panes:
                    pane.setdefault("host_id", "localhost")
                    pane.setdefault("window_name", "")
                target.update({"panes": panes, "status": "online"})
                target.pop("error", None)
            except (TmuxError, OSError) as exc:
                target.update({"panes": [], "status": "offline", "error": str(exc)})
        else:
            timeout = int(os.environ.get("TMUX_WEB_HOSTS_LIST_TIMEOUT", "8"))
            try:
                credentialed = await asyncio.to_thread(remote_hosts.get_host, host_id, include_credential=True)
                panes = await asyncio.to_thread(runner_for(credentialed).list_panes, timeout=timeout)
                target.update({"panes": [pane_metadata(pane) for pane in panes], "status": "online"})
                target.pop("error", None)
            except Exception as exc:  # noqa: BLE001 - report it on the host row
                target.update({"panes": [], "status": "offline", "error": str(exc)})
        hosts_snapshot["value"] = fresh
        hosts_snapshot["at"] = time.time()
        return fresh

    @app.get("/api/hosts")
    async def list_hosts(request: Request, refresh: int = 0, host: str = ""):
        require_auth(request)
        # every 5s poll used to re-probe each remote host over SSH; a 5s snapshot
        # keeps the sidebar fresh without hammering them (?refresh=1 forces it,
        # &host=<id> re-lists only that host so one click stays cheap)
        cached = hosts_snapshot["value"]
        if host and refresh and cached is not None:
            refreshed = await refresh_one_host(host, cached)
            if refreshed is not None:
                return refreshed
        if cached is not None and not refresh and (time.time() - hosts_snapshot["at"]) < hosts_snapshot["ttl"]:
            return cached
        hosts = []
        try:
            panes = [pane_metadata(pane) for pane in tmux.list_panes()]
            for pane in panes:
                pane.setdefault("host_id", "localhost")
                pane.setdefault("window_name", "")
            hosts.append({
                "id": "localhost",
                "name": "localhost",
                "kind": "local",
                "status": "online",
                "panes": panes,
            })
        except (TmuxError, OSError) as exc:
            hosts.append({"id": "localhost", "name": "localhost", "kind": "local",
                          "status": "offline", "error": str(exc), "panes": []})
        remotes = remote_hosts.list_hosts()
        list_timeout = int(os.environ.get("TMUX_WEB_HOSTS_LIST_TIMEOUT", "8"))
        public_hosts = []
        for remote in remotes:
            public = {key: remote[key] for key in ("id", "name", "hostname", "port", "username", "auth_type", "fingerprint")}
            public.update({"kind": "ssh", "status": "offline", "panes": []})
            public_hosts.append(public)

        async def load_remote(public: dict) -> None:
            try:
                credentialed = remote_hosts.get_host(public["id"], include_credential=True)
                panes = await asyncio.to_thread(
                    runner_for(credentialed).list_panes, timeout=list_timeout
                )
                public["panes"] = [pane_metadata(pane) for pane in panes]
                public["status"] = "online"
            except Exception as exc:  # noqa: BLE001 - one dead host must not break the tree
                # SSH failures surface as RuntimeError/OSError, but a hung host
                # raises subprocess.TimeoutExpired and httpx raises its own types.
                public["error"] = str(exc)

        # Concurrently, so the slowest host sets the latency instead of the sum.
        await asyncio.gather(*(load_remote(public) for public in public_hosts))
        hosts.extend(public_hosts)
        payload = {"hosts": hosts}
        hosts_snapshot["value"] = payload
        hosts_snapshot["at"] = time.time()
        return payload

    async def perform_mcp_tool(name: str, arguments: dict) -> dict:
        """Same semantics as mcp_relay.py, but executed in-process (no python3 on the client)."""
        arguments = arguments or {}
        if name == "list_remote_hosts":
            hosts = await asyncio.to_thread(remote_hosts.list_hosts)
            return {"hosts": [
                {"id": host.get("id"), "name": host.get("name"), "hostname": host.get("hostname"),
                 "port": host.get("port"), "username": host.get("username"),
                 "auth_type": host.get("auth_type"), "jump_host_id": host.get("jump_host_id") or "",
                 "jump_path": " -> ".join((hop.get("name") if hop.get("inline")
                                           else hop.get("name") or hop["id"])
                                          for hop in remote_hosts.jump_chain(host, limit=jump_max_depth)),
                 "jump_spec": host.get("jump_spec") or "",
                 "tmux_path": host.get("tmux_path") or "",
                 "fingerprint": host.get("fingerprint") or ""}
                for host in hosts
            ]}
        if name == "add_remote_host":
            auth_type = str(arguments.get("auth_type") or "agent").strip() or "agent"
            if auth_type not in AUTH_TYPES:
                raise McpToolError("auth_type must be agent, password or key")
            payload = RemoteHostCreateRequest(
                name=str(arguments.get("name") or "").strip(),
                hostname=str(arguments.get("hostname") or "").strip(),
                username=str(arguments.get("username") or "").strip(),
                port=int(arguments.get("port") or 22),
                auth_type=auth_type,
                credential=str(arguments.get("credential") or ""),
                jump_host_id=str(arguments.get("jump_host") or "").strip(),
                jump_spec=str(arguments.get("jump") or "").strip(),
                verify=bool(arguments.get("verify", True)),
            )
            try:
                created = await store_remote_host(payload)
            except HTTPException as exc:
                raise McpToolError(str(exc.detail)) from exc
            return {"host": {key: created.get(key) for key in
                             ("id", "name", "hostname", "port", "username", "auth_type",
                              "jump_host_id", "jump_spec")},
                    "note": "the console sidebar picks it up on its next refresh"}
        if name == "remove_remote_host":
            host_id = str(arguments.get("id") or "").strip()
            if not host_id:
                raise McpToolError("id is required")
            if not await asyncio.to_thread(remote_hosts.delete_host, host_id):
                raise McpToolError("no such host")
            return {"id": host_id, "removed": True}
        if name == "publish_home_card":
            title = str(arguments.get("title") or "").strip()
            if not title:
                raise McpToolError("title is required")
            try:
                card = await asyncio.to_thread(home.add_card, title, str(arguments.get("body") or ""),
                                               str(arguments.get("pane") or ""), str(arguments.get("kind") or "note"))
            except ValueError as exc:
                raise McpToolError(str(exc)) from exc
            return {"card": card, "visible_at": "/home"}
        if name == "list_home":
            payload = await build_home_payload()
            limit = max(1, min(int(arguments.get("limit") or 20), 60))
            payload["cards"] = payload["cards"][:limit]
            payload["favorites"] = payload["favorites"][:limit]
            return payload
        if name == "remove_home_card":
            card_id = str(arguments.get("id") or "").strip()
            if not card_id:
                raise McpToolError("id is required")
            if not await asyncio.to_thread(home.delete_card, card_id):
                raise McpToolError("no such card")
            return {"id": card_id, "removed": True}
        if name == "list_agents":
            peers = await asyncio.to_thread(collect_pane_states)
            if not arguments.get("include_non_agents", False):
                peers = [peer for peer in peers if peer.get("provider")]
            return {"agents": [
                {
                    "target": peer["id"],
                    "host": peer.get("host_name") or peer.get("host_id"),
                    "session": (peer.get("target") or "").split(":")[0],
                    "window": peer.get("target"),
                    "provider": peer.get("provider"),
                    "status": peer.get("status"),
                    "path": peer.get("path"),
                    "title": peer.get("title"),
                }
                for peer in peers
            ]}
        if name == "ask_agent":
            target = str(arguments.get("target") or "").strip()
            question = str(arguments.get("question") or "").strip()
            if not target or not question:
                raise McpToolError("target and question are required")
            timeout = max(5, min(int(arguments.get("timeout") or 240), 600))
            try:
                result = await perform_relay_ask(RelayAskRequest(
                    target=target, question=question[:20000], timeout=timeout,
                    force=bool(arguments.get("force", False)),
                    source=str(arguments.get("source") or "mcp"),
                ))
            except RelayTimeoutError as exc:
                raise McpToolError(str(exc), exc.tail) from exc
            except HTTPException as exc:
                raise McpToolError(str(exc.detail)) from exc
            return {"target": target, "reply": result.get("reply", ""), "pane": result.get("pane")}
        if name == "send_agent":
            target = str(arguments.get("target") or "").strip()
            text = str(arguments.get("text") or "").strip()
            if not target or not text:
                raise McpToolError("target and text are required")
            transport, pane_id = await asyncio.to_thread(pane_transport, target)
            try:
                await asyncio.to_thread(transport.send, pane_id, text[:20000], bool(arguments.get("submit", True)))
            except (TmuxError, RuntimeError, OSError, ValueError) as exc:
                raise McpToolError(str(exc)) from exc
            return {"target": target, "sent": True}
        if name == "read_agent":
            target = str(arguments.get("target") or "").strip()
            if not target:
                raise McpToolError("target is required")
            transport, pane_id = await asyncio.to_thread(pane_transport, target)
            try:
                if arguments.get("transcript"):
                    limit = max(1, min(int(arguments.get("limit") or 50), 300))
                    data = await asyncio.to_thread(transport.transcript, pane_id, limit)
                    return {"target": target, "provider": data.get("provider"),
                            "session": data.get("session_id"), "messages": data.get("messages", [])}
                history = max(10, min(int(arguments.get("lines") or 80), 5000))
                content = await asyncio.to_thread(transport.capture, pane_id, history)
                return {"target": target, "content": content}
            except (TmuxError, RuntimeError, OSError, ValueError) as exc:
                raise McpToolError(str(exc)) from exc
        raise McpToolError("unknown tool: %s" % name)

    def require_mcp_auth(request: Request) -> None:
        # LAN-only deployments can skip the bearer header: TMUX_WEB_MCP_AUTH=off
        if mcp_auth_required:
            require_auth(request)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """Streamable-HTTP MCP endpoint: agents point a URL here instead of spawning python3."""
        require_mcp_auth(request)
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "invalid JSON"}}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "batch requests are not supported"}},
                                status_code=400)
        request_id = payload.get("id")
        method = payload.get("method")
        if request_id is None:
            # notifications (initialized, cancelled, ...) need no body
            return Response(status_code=202)

        def reply(result: dict) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

        def tool_text(value: object, *, is_error: bool = False) -> dict:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return {"content": [{"type": "text", "text": text}], "isError": is_error}

        if method == "initialize":
            return reply({"protocolVersion": MCP_PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "tmuxweb-relay", "version": "1.0.0"}})
        if method == "ping":
            return reply({})
        if method == "tools/list":
            return reply({"tools": mcp_relay.tool_schemas()})
        if method == "tools/call":
            params = payload.get("params") or {}
            try:
                result = await perform_mcp_tool(str(params.get("name") or ""), params.get("arguments") or {})
            except McpToolError as exc:
                detail: dict = {"error": str(exc)}
                if exc.tail:
                    detail["pane_tail"] = exc.tail
                return reply(tool_text(detail, is_error=True))
            return reply(tool_text(result))
        return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                             "error": {"code": -32601, "message": "Method not found"}}, status_code=404)

    @app.get("/mcp")
    async def mcp_get(request: Request):
        require_mcp_auth(request)
        raise HTTPException(status_code=405, detail="this MCP endpoint only accepts POST",
                            headers={"Allow": "POST"})

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request, payload: SessionCreateRequest):
        """Create a detached tmux session on the local host or a remote one."""
        require_auth(request)
        if payload.host in ("", "localhost"):
            transport, host_id = tmux, "localhost"
        else:
            try:
                host = remote_hosts.get_host(payload.host, include_credential=True)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="remote host not found") from exc
            transport, host_id = runner_for(host), payload.host
        try:
            created = await asyncio.to_thread(transport.create_session, payload.name, payload.path)
            panes = await asyncio.to_thread(transport.list_panes)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            status = 409 if ("already exists" in message or "duplicate" in message) else 422
            if status == 422 and "no such directory" not in message and "session name" not in message:
                status = 503
            raise HTTPException(status_code=status, detail=message) from exc
        session = created["session"]
        fresh = []
        for pane in panes:
            if pane.get("session") != session:
                continue
            metadata = pane_metadata(pane)
            if host_id != "localhost":
                metadata["id"] = pane_ref(host_id, metadata.get("pane_id") or metadata["id"])
            metadata.setdefault("host_id", host_id)
            fresh.append(metadata)
        return {"session": created, "host": host_id, "panes": fresh}

    @app.post("/api/remote-hosts/scan-key")
    async def scan_remote_key(request: Request):
        require_auth(request)
        body = await request.json()
        hostname = str(body.get("hostname", "")).strip()
        port = int(body.get("port", 22))
        if not hostname or not 1 <= port <= 65535:
            raise HTTPException(status_code=422, detail="invalid hostname or port")
        try:
            return await asyncio.to_thread(scan_host_key, hostname, port)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise HTTPException(status_code=424, detail=str(exc)) from exc

    async def store_remote_host(payload: RemoteHostCreateRequest) -> dict:
        """Validate and persist a host; shared by the HTTP route and the MCP tool."""
        if payload.auth_type not in AUTH_TYPES:
            raise HTTPException(status_code=422, detail="auth_type must be password, key or agent")
        if payload.auth_type == "key":
            try:
                validate_private_key_text(payload.credential)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.auth_type in {"password", "key"} and not payload.credential:
            raise HTTPException(status_code=422, detail="password or private key is required")
        if payload.verify and not (payload.host_key and payload.fingerprint):
            raise HTTPException(status_code=422, detail="SSH host key is required unless verification is skipped")
        if payload.host_key or payload.fingerprint:
            if not payload.host_key or not payload.fingerprint:
                raise HTTPException(status_code=422, detail="host_key and fingerprint must be provided together")
            try:
                computed = host_key_fingerprint(payload.host_key)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not hmac.compare_digest(computed, payload.fingerprint) or (
                payload.verify and not hmac.compare_digest(payload.fingerprint, payload.confirm_fingerprint)
            ):
                raise HTTPException(status_code=409, detail="SSH host key fingerprint was not confirmed")
        try:
            host = remote_hosts.add_host(
                name=payload.name, hostname=payload.hostname, port=payload.port, username=payload.username,
                auth_type=payload.auth_type, credential=payload.credential, host_key=payload.host_key,
                fingerprint=payload.fingerprint, jump_host_id=payload.jump_host_id,
                jump_spec=payload.jump_spec,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.verify:
            try:
                credentialed = remote_hosts.get_host(host["id"], include_credential=True)
                await asyncio.to_thread(runner_for(credentialed).check)
            except (RuntimeError, OSError) as exc:
                remote_hosts.delete_host(host["id"])
                raise HTTPException(status_code=424, detail=str(exc)) from exc
        return host

    @app.post("/api/remote-hosts", status_code=201)
    async def create_remote_host(request: Request, payload: RemoteHostCreateRequest):
        require_auth(request)
        return await store_remote_host(payload)

    @app.post("/api/remote-hosts/test")
    async def test_remote_host(request: Request, payload: RemoteHostCreateRequest):
        require_auth(request)
        if payload.auth_type not in AUTH_TYPES:
            raise HTTPException(status_code=422, detail="auth_type must be password, key or agent")
        if payload.auth_type == "key":
            try:
                validate_private_key_text(payload.credential)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.auth_type in {"password", "key"} and not payload.credential:
            raise HTTPException(status_code=422, detail="password or private key is required")
        try:
            computed = host_key_fingerprint(payload.host_key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not hmac.compare_digest(computed, payload.fingerprint) or not hmac.compare_digest(
            payload.fingerprint, payload.confirm_fingerprint
        ):
            raise HTTPException(status_code=409, detail="SSH host key fingerprint was not confirmed")
        temporary_host = {
            "id": "0000000000000000", "name": payload.name, "hostname": payload.hostname,
            "port": payload.port, "username": payload.username, "auth_type": payload.auth_type,
            "credential": payload.credential, "host_key": payload.host_key,
            "fingerprint": payload.fingerprint, "enabled": True,
        }
        try:
            runner = make_remote_runner(temporary_host)
            banner = await asyncio.to_thread(runner.check)
            panes = await asyncio.to_thread(runner.list_panes)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise HTTPException(status_code=424, detail=str(exc)) from exc
        return {"ssh_ok": "TMUX_REMOTE_OK" in banner, "tmux_ok": True,
                "pane_count": len(panes), "sessions": sorted({pane["session"] for pane in panes})}

    @app.put("/api/remote-hosts/{host_id}")
    async def update_remote_host(request: Request, host_id: str, payload: RemoteHostUpdateRequest):
        require_auth(request)
        try:
            previous = remote_hosts.get_host(host_id, include_credential=True)
        except KeyError:
            raise HTTPException(status_code=404, detail="remote host not found") from None
        if payload.auth_type is not None and payload.auth_type not in AUTH_TYPES:
            raise HTTPException(status_code=422, detail="auth_type must be password, key or agent")
        host_key = payload.host_key
        fingerprint = payload.fingerprint
        if host_key or fingerprint:
            if not host_key or not fingerprint:
                raise HTTPException(status_code=422, detail="host_key and fingerprint must be provided together")
            try:
                computed = host_key_fingerprint(host_key)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not hmac.compare_digest(computed, fingerprint):
                raise HTTPException(status_code=409, detail="SSH host key fingerprint does not match the key")
        try:
            host = remote_hosts.update_host(
                host_id,
                name=payload.name, hostname=payload.hostname, port=payload.port,
                username=payload.username, auth_type=payload.auth_type,
                credential=payload.credential, host_key=host_key, fingerprint=fingerprint,
                jump_host_id=payload.jump_host_id, jump_spec=payload.jump_spec,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if payload.verify is False:
            return host
        try:
            credentialed = remote_hosts.get_host(host_id, include_credential=True)
            await asyncio.to_thread(runner_for(credentialed).check)
        except (RuntimeError, OSError) as exc:
            # keep what was working: an edit should never silently break a host
            remote_hosts.update_host(
                host_id, name=previous["name"], hostname=previous["hostname"], port=previous["port"],
                username=previous["username"], auth_type=previous["auth_type"],
                credential=previous["credential"], host_key=previous["host_key"],
                fingerprint=previous["fingerprint"], jump_host_id=previous.get("jump_host_id", ""),
                jump_spec=previous.get("jump_spec", ""), tmux_path=previous.get("tmux_path", ""),
            )
            raise HTTPException(status_code=424, detail=str(exc)) from exc
        return host

    @app.post("/api/remote-hosts/reorder")
    async def reorder_remote_hosts(request: Request, payload: RemoteHostReorderRequest):
        require_auth(request)
        try:
            hosts = remote_hosts.reorder_hosts(payload.ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"hosts": hosts}

    @app.get("/api/remote-hosts")
    async def list_remote_hosts(request: Request):
        require_auth(request)
        stored = remote_hosts.list_hosts()
        names = {host["id"]: host.get("name") or host["id"] for host in stored}
        result = []
        for host in stored:
            item = {key: host.get(key, "") for key in (
                "id", "name", "hostname", "port", "username", "auth_type", "fingerprint", "created_at",
                "enabled", "position", "jump_host_id", "jump_spec", "tmux_path",
            )}
            chain = remote_hosts.jump_chain(host, limit=jump_max_depth)
            item["jump_chain"] = [{"id": hop["id"], "name": hop.get("name") or hop["id"],
                                   "hostname": hop.get("hostname")}
                                  for hop in chain]
            item["jump_path"] = " -> ".join((hop.get("name") if hop.get("inline")
                                             else names.get(hop["id"], hop.get("name") or hop["id"]))
                                            for hop in chain)
            result.append(item)
        return {"hosts": result}

    @app.delete("/api/remote-hosts/{host_id}")
    async def delete_remote_host(request: Request, host_id: str):
        require_auth(request)
        if not remote_hosts.delete_host(host_id):
            raise HTTPException(status_code=404, detail="remote host not found")
        return {"status": "deleted", "id": host_id}

    @app.get("/api/panes/{pane_id}/capture")
    async def capture(request: Request, pane_id: str, history: int = 500):
        require_auth(request)
        pane_or_404(pane_id)
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            content = await asyncio.to_thread(transport.capture, actual_pane_id, history)
            return {"pane": pane_id, "content": content}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def with_waiting_hint(data: dict) -> dict:
        """Tell the UI whether the agent is still working on the last message.

        A trailing user message means the turn has not been answered yet; a
        transcript that is still being written (recent mtime) means the agent is
        streaming something we parse only when it completes.
        """
        messages = data.get("messages") or []
        last = messages[-1] if messages else {}
        # an unanswered user turn is the one signal a transcript can give cheaply;
        # agents that stream (codex) are covered by the pane-state probe instead
        waiting = (last.get("role") == "user")
        since = last.get("timestamp") if waiting else None
        data["waiting"] = bool(waiting)
        data["waiting_since"] = since
        return data

    @app.get("/api/panes/{pane_id}/transcript")
    async def transcript(request: Request, pane_id: str, limit: int = 200, refresh: int = 0):
        require_auth(request)
        pane_or_404(pane_id)
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            if refresh and hasattr(transport, "transcript"):
                data = await asyncio.to_thread(transport.transcript, actual_pane_id, limit, True)
            else:
                data = await asyncio.to_thread(transport.transcript, actual_pane_id, limit)
            return with_waiting_hint(data)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/panes/{pane_id}/input")
    async def send_input(request: Request, pane_id: str, payload: InputRequest):
        require_auth(request)
        pane_or_404(pane_id)
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            await asyncio.to_thread(transport.send, actual_pane_id, payload.text, payload.submit)
            return {"status": "sent", "pane": pane_id, "submit": payload.submit}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/panes/{pane_id}/interrupt")
    async def interrupt_pane(request: Request, pane_id: str):
        """Stop the turn that is running in this pane, using the key its CLI expects."""
        require_auth(request)
        pane = pane_or_404(pane_id)
        provider = transcript_reader.provider_for_command(pane.get("command"))
        key = interrupt_key(provider)
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            await asyncio.to_thread(transport.send_key, actual_pane_id, key)
            return {"status": "sent", "pane": pane_id, "key": key, "provider": provider}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/panes/{pane_id}/key")
    async def send_key(request: Request, pane_id: str, payload: KeyRequest):
        """Send one named key (Escape interrupts a running agent turn)."""
        require_auth(request)
        pane_or_404(pane_id)
        if payload.key not in ALLOWED_KEYS:
            raise HTTPException(status_code=422, detail="unsupported key")
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            await asyncio.to_thread(transport.send_key, actual_pane_id, payload.key)
            return {"status": "sent", "pane": pane_id, "key": payload.key}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def file_target(pane_ref: str) -> tuple[object, str, dict]:
        pane = pane_or_404(pane_ref)
        transport, actual_pane_id = pane_transport(pane_ref)
        return transport, actual_pane_id, pane

    @app.get("/api/panes/{pane_id}/files")
    async def list_pane_files(request: Request, pane_id: str, path: str = "", hidden: int = 0,
                              limit: int = 2000):
        require_auth(request)
        try:
            transport, _actual, pane = file_target(pane_id)
            directory = path or pane.get("path") or "."
            data = await asyncio.to_thread(transport.list_files, directory, bool(hidden),
                                           max(1, min(limit, 5000)))
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        data["pane"] = pane_id
        return data

    @app.get("/api/panes/{pane_id}/files/raw")
    async def raw_pane_file(request: Request, pane_id: str, path: str):
        """Serve a file inline so markdown links to local paths can be opened.

        Only text/images/PDF are previewed; anything else is still safe to show
        because the response is inert (no scripts run from this origin).
        """
        require_auth(request)
        if not path:
            raise HTTPException(status_code=422, detail="path is required")
        try:
            transport, _actual, _pane = file_target(pane_id)
            payload = await asyncio.to_thread(transport.download_file, path, download_limit)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            if "not a file" in message or "no such file" in message.lower():
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=503, detail=message) from exc
        name = os.path.basename(path.rstrip("/")) or "file"
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline_types = ("text/", "image/", "application/pdf", "application/json")
        inline = media_type.startswith(inline_types)
        headers = {
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
        }
        if inline:
            headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(name, safe="")
        else:
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name, safe="")
        return Response(content=payload, media_type=media_type, headers=headers)

    @app.get("/api/panes/{pane_id}/files/download")
    async def download_pane_file(request: Request, pane_id: str, path: str):
        require_auth(request)
        if not path:
            raise HTTPException(status_code=422, detail="path is required")
        try:
            transport, _actual, _pane = file_target(pane_id)
            payload = await asyncio.to_thread(transport.download_file, path, download_limit)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        name = os.path.basename(path.rstrip("/")) or "download"
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(
            content=payload,
            media_type=media_type,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/panes/{pane_id}/files/zip")
    async def zip_pane_files(request: Request, pane_id: str, payload: ZipRequest):
        require_auth(request)
        wanted = [item for item in (payload.paths or []) if item.strip()]
        if not wanted:
            raise HTTPException(status_code=422, detail="select at least one file")
        if len(wanted) > 1000:
            raise HTTPException(status_code=422, detail="too many paths selected")
        try:
            transport, _actual, pane = file_target(pane_id)
            base = payload.base or pane.get("path") or "."
            blob = await asyncio.to_thread(transport.zip_files, wanted, base, zip_limit, zip_max_files)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        name = (os.path.basename(str(base).rstrip("/")) or "files") + ".zip"
        return Response(
            content=blob,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
                "Content-Length": str(len(blob)),
                "Cache-Control": "no-store",
            },
        )

    @app.put("/api/panes/{pane_id}/files/upload")
    async def upload_pane_file(request: Request, pane_id: str, name: str, dir: str = ""):
        require_auth(request)
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise HTTPException(status_code=422, detail="invalid file name")
        try:
            transport, _actual, pane = file_target(pane_id)
            directory = dir or pane.get("path") or "."
            body = await request.body()
            if len(body) > upload_limit:
                raise HTTPException(status_code=413,
                                    detail=f"file exceeds the upload limit of {upload_limit} bytes")
            target = os.path.join(directory, name)
            await asyncio.to_thread(transport.upload_file, target, body)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "uploaded", "path": target, "size": len(body)}

    @app.get("/api/relay/peers")
    async def relay_peers(request: Request):
        require_auth(request)
        return {"peers": await asyncio.to_thread(collect_pane_states)}

    def host_label(pane_id: str) -> str:
        try:
            host_id, _ = split_pane_ref(pane_id)
        except ValueError:
            return ""
        if host_id == "localhost":
            return "localhost"
        try:
            return remote_hosts.get_host(host_id).get("name") or host_id
        except KeyError:
            return host_id

    def default_card_title(pane: dict, host_name: str) -> str:
        """session · window:name · host — the pane title is often just the hostname."""
        session = pane.get("session") or ""
        window = pane.get("window") or ""
        window_name = pane.get("window_name") or ""
        window_label = f"{window}: {window_name}" if window and window_name else (window or window_name)
        parts = [part for part in (session, window_label, host_name) if part]
        return " · ".join(parts)

    def home_entry_skeleton(row: dict) -> dict:
        """What a card can say before any live read: no tmux, no SSH, no I/O."""
        pane_id = str(row.get("pane_id") or "")
        host_id = pane_id.split("~", 1)[0] if "~" in pane_id else "localhost"
        custom_title = row.get("title") or ""
        try:
            host_name = host_id if host_id == "localhost" else (remote_hosts.get_host(host_id)["name"] or host_id)
        except (KeyError, RuntimeError, OSError, ValueError):
            host_name = host_id
        return {"pane": pane_id, "note": row.get("note") or "", "status": "unknown", "provider": None,
                "title": custom_title, "default_title": custom_title or pane_id,
                "custom_title": custom_title != "", "pane_title": "", "path": "", "session": "",
                "window": "", "host": host_name, "command": "", "preview": None, "agent_preview": None,
                "waiting": False, "favorite": True, "pending": True}

    def home_entry(pane_id: str, note: str = "", custom_title: str = "") -> dict:
        """Everything a Home card needs about one pane; failures degrade to offline."""
        entry: dict = {"pane": pane_id, "note": note, "status": "offline", "provider": None,
                       "title": custom_title, "default_title": "", "custom_title": custom_title != "",
                       "pane_title": "", "path": "", "session": "", "window": "", "host": "",
                       "command": "", "preview": None, "agent_preview": None,
                       "waiting": False, "favorite": True}
        try:
            pane = pane_or_404(pane_id)
            transport, actual_pane_id = pane_transport(pane_id)
        except HTTPException:
            return entry
        provider = transcript_reader.provider_for_command(pane.get("command"))
        host_name = host_label(pane_id)
        entry.update({"provider": provider, "pane_title": pane.get("title") or "",
                      "path": pane.get("path") or "", "session": pane.get("session") or "",
                      "window": pane.get("target") or "", "command": pane.get("command") or "",
                      "host": host_name})
        entry["default_title"] = default_card_title(pane, host_name) or (pane.get("title") or pane_id)
        if not custom_title:
            entry["title"] = entry["default_title"]
        try:
            entry["status"] = relay.inspect(transport, actual_pane_id, provider, lines=30)["status"]
        except (RuntimeError, OSError, ValueError):
            entry["status"] = "unknown"
        if not provider:
            return entry
        try:
            data = transport.transcript(actual_pane_id, 4)
        except (RuntimeError, OSError, ValueError, transcript_reader.TranscriptError):
            return entry
        messages = data.get("messages") or []
        for message in reversed(messages):
            text = " ".join((message.get("text") or "").split())
            if text:
                entry["preview"] = {"role": message.get("role") or "", "text": text[:400],
                                    "timestamp": message.get("timestamp")}
                break
        # the hover card shows the last *agent* turn, so a trailing user prompt
        # (or a queued one) does not hide what the agent actually said
        for message in reversed(messages):
            role = (message.get("role") or "").lower()
            text = (message.get("text") or "").strip()
            if role == "user" or not text:
                continue
            entry["agent_preview"] = {
                "role": role,
                "text": text[:AGENT_PREVIEW_CHARS],
                "truncated": len(text) > AGENT_PREVIEW_CHARS,
                "timestamp": message.get("timestamp"),
            }
            break
        try:
            entry["waiting"] = bool(with_waiting_hint(data).get("waiting"))
        except (RuntimeError, ValueError):
            entry["waiting"] = False
        # the provider's own transcript knows whether a turn is running; the screen
        # heuristic can be fooled by status bars ("K3.1 Pro thinking: high")
        entry["status"] = "busy" if entry["waiting"] else "idle"
        entry["status_source"] = "transcript"
        # ...and the sweep already asked each session file directly: that verdict wins
        # over a transcript read that may be a page behind
        with pane_state_lock:
            swept = list(pane_state_cache["states"])
        for state in swept:
            if state.get("id") == pane_id and state.get("status_source") == "session" \
                    and state.get("status") in ("busy", "idle"):
                entry["status"] = state["status"]
                entry["status_source"] = "session"
                break
        return entry

    # A card's live half (status + last message) costs one SSH round trip per pane,
    # which is seconds on a remote host. Entries are cached and the request waits at
    # most HOME_ENTRY_BUDGET before answering with placeholders; the slow thread
    # keeps running and fills the cache for the next poll.
    HOME_ENTRY_CACHE: dict[str, tuple[float, dict]] = {}
    HOME_ENTRY_TTL = float(os.environ.get("TMUX_WEB_HOME_ENTRY_SECONDS", "25"))
    HOME_ENTRY_BUDGET = float(os.environ.get("TMUX_WEB_HOME_BUDGET_SECONDS", "1.2"))

    def remember_home_entry(pane_id: str, entry: dict) -> None:
        # a failed read (unknown/offline, no preview) is retried soon instead of
        # being trusted for the full TTL
        ttl = HOME_ENTRY_TTL if (entry.get("preview") or entry.get("status") in ("busy", "idle")) else 5.0
        HOME_ENTRY_CACHE[pane_id] = (time.time(), entry, ttl)
        if len(HOME_ENTRY_CACHE) > 200:  # bounded
            oldest = min(HOME_ENTRY_CACHE, key=lambda key: HOME_ENTRY_CACHE[key][0])
            HOME_ENTRY_CACHE.pop(oldest, None)

    def collect_home_entry(future, pane_id: str) -> None:
        try:
            entry = future.result()
        except Exception:  # noqa: BLE001 - a failed read just leaves the placeholder
            return
        remember_home_entry(pane_id, entry)

    def with_cached_preview(pane_id: str, entry: dict) -> dict:
        """A pane that cannot be read still has a last known answer: keep showing it.

        The cache holds the previous entry (and when it was taken) so a dropped host does not blank
        the card; the client marks it offline and shows how old the cached text is.
        """
        if entry.get("preview") or entry.get("agent_preview"):
            return entry
        hit = HOME_ENTRY_CACHE.get(pane_id)
        if not hit:
            return entry
        previous = hit[1] or {}
        if not (previous.get("preview") or previous.get("agent_preview")):
            return entry
        merged = dict(entry)
        for key in ("preview", "agent_preview", "waiting", "provider"):
            if previous.get(key):
                merged[key] = previous[key]
        merged["cached"] = True
        merged["cached_at"] = hit[0]
        if not merged.get("status"):
            merged["status"] = "offline"
        return merged

    async def build_home_payload() -> dict:
        started = time.time()
        favorites = await asyncio.to_thread(home.favorites)
        loop = asyncio.get_running_loop()
        slots: list = [None] * len(favorites)
        futures: dict = {}
        for index, row in enumerate(favorites):
            hit = HOME_ENTRY_CACHE.get(row["pane_id"])
            if hit and (time.time() - hit[0]) < (hit[2] if len(hit) > 2 else HOME_ENTRY_TTL):
                slots[index] = hit[1]
                continue
            future = loop.run_in_executor(None, home_entry, row["pane_id"],
                                          row.get("note") or "", row.get("title") or "")
            futures[future] = index
        if futures:
            done, _ = await asyncio.wait(futures, timeout=HOME_ENTRY_BUDGET)
            for future in done:
                row = favorites[futures[future]]
                try:
                    entry = future.result()
                except Exception:  # noqa: BLE001
                    entry = None
                if isinstance(entry, dict):
                    if entry.get("status") == "offline":
                        entry = with_cached_preview(row["pane_id"], entry)
                    remember_home_entry(row["pane_id"], entry)
                    slots[futures[future]] = entry
                else:
                    slots[futures[future]] = with_cached_preview(row["pane_id"], home_entry_skeleton(row))
            for future in set(futures) - done:
                row = favorites[futures[future]]
                slots[futures[future]] = with_cached_preview(row["pane_id"], home_entry_skeleton(row))
                future.add_done_callback(lambda fut, pane=row["pane_id"]: collect_home_entry(fut, pane))
        cards = await asyncio.to_thread(home.cards)
        # counters may lag a few seconds; the favourites above are always live
        states = await asyncio.to_thread(pane_states_cached, 0.4, 30.0)
        elapsed = time.time() - started
        (LOGGER.warning if elapsed > 2 else LOGGER.info)(
            "home payload in %.2fs (%d favourites, %d pending)", elapsed, len(favorites),
            sum(1 for entry in slots if entry and entry.get("pending")))
        busy = sum(1 for state in states if state.get("status") == "busy")
        recent_rows = await asyncio.to_thread(home.recent, 4)
        by_id = {state["id"]: state for state in states}
        # titles/hosts come from the /api/hosts snapshot too: it is cheap and warm
        # even when the status sweep has not run yet
        known: dict[str, dict] = {}
        for host in (hosts_snapshot["value"] or {}).get("hosts") or []:
            for pane in host.get("panes") or []:
                known[pane.get("id")] = {**pane, "host_name": host.get("name") or host.get("id")}
        recent = []
        for row in recent_rows:
            pane_id = row["pane_id"]
            state = by_id.get(pane_id) or {}
            pane = known.get(pane_id) or {}
            host_id = pane_id.split("~", 1)[0] if "~" in pane_id else "localhost"
            recent.append({
                "pane": pane_id,
                "visited_at": row["visited_at"],
                "title": state.get("title") or pane.get("title") or pane_id,
                "host": state.get("host_name") or pane.get("host_name") or host_id,
                "session": state.get("session") or pane.get("session") or "",
                "window": state.get("window") or pane.get("target") or "",
                "provider": state.get("provider") or "",
                # a visit whose pane is gone should read as offline, not "unknown"
                "status": state.get("status") or ("unknown" if pane else "offline"),
                "online": bool(state or pane),
            })
        return {
            "favorites": [entry for entry in slots if isinstance(entry, dict)],
            "recent": recent,
            "cards": cards,
            "stats": {
                "panes": len(states),
                "busy": busy,
                "idle": sum(1 for state in states if state.get("status") == "idle"),
                "favorites": len(favorites),
                "cards": len(cards),
            },
            "generated_at": time.time(),
        }

    @app.get("/api/home")
    async def home_board(request: Request):
        require_auth(request)
        return await build_home_payload()

    @app.post("/api/home/recent", status_code=202)
    async def touch_home_recent(request: Request, payload: HomeRecentRequest):
        """Remember that a pane was opened (called by the console on selection)."""
        require_auth(request)
        try:
            split_pane_ref(payload.pane)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid pane reference") from exc
        await asyncio.to_thread(home.touch_recent, payload.pane)
        return {"pane": payload.pane, "status": "recorded"}

    @app.delete("/api/home/recent/{pane_id}")
    async def delete_home_recent(request: Request, pane_id: str):
        require_auth(request)
        await asyncio.to_thread(home.forget_recent, pane_id)
        return {"status": "deleted", "pane": pane_id}

    @app.get("/api/home/favorites")
    async def home_favorites(request: Request):
        require_auth(request)
        rows = await asyncio.to_thread(home.favorites)
        return {"favorites": [row["pane_id"] for row in rows], "notes": {row["pane_id"]: row["note"] for row in rows}}

    def forget_home_entry(pane_id: str) -> None:
        HOME_ENTRY_CACHE.pop(pane_id, None)

    @app.post("/api/home/favorites", status_code=201)
    async def add_home_favorite(request: Request, payload: HomeFavoriteRequest):
        require_auth(request)
        # a well-formed reference is enough: a host that is momentarily
        # unreachable must not stop you from bookmarking its pane
        try:
            split_pane_ref(payload.pane)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid pane reference") from exc
        try:
            row = await asyncio.to_thread(home.add_favorite, payload.pane, payload.note, payload.title)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        forget_home_entry(payload.pane)
        return {"pane": row["pane_id"], "note": row["note"], "title": row.get("title", ""), "favorite": True}

    @app.patch("/api/home/favorites/{pane_id}")
    async def rename_home_favorite(request: Request, pane_id: str, payload: HomeFavoriteTitleRequest):
        """Rename a card; an empty title restores the generated one."""
        require_auth(request)
        try:
            row = await asyncio.to_thread(home.set_favorite_title, pane_id, payload.title)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="pane is not on the board") from exc
        forget_home_entry(pane_id)
        return {"pane": row["pane_id"], "title": row.get("title", ""), "favorite": True}

    @app.delete("/api/home/favorites/{pane_id}")
    async def remove_home_favorite(request: Request, pane_id: str):
        require_auth(request)
        removed = await asyncio.to_thread(home.remove_favorite, pane_id)
        return {"pane": pane_id, "favorite": False, "removed": removed}

    class QueryMatcher:
        """Case-insensitive regex when the query looks like one, literal otherwise.

        Plain words stay literal (so "a.b" does not match "axb"), anything with
        regex metacharacters is compiled, and a broken pattern falls back to a
        literal search instead of failing the request.
        """

        METACHARS = set(".*+?[](){}|^$\\")

        def __init__(self, query: str):
            self.query = query
            looks_like_regex = any(char in self.METACHARS for char in query)
            try:
                self.regex = re.compile(query if looks_like_regex else re.escape(query), re.IGNORECASE)
                self.mode = "regex" if looks_like_regex else "literal"
            except re.error:
                self.regex = re.compile(re.escape(query), re.IGNORECASE)
                self.mode = "literal-fallback"

        def __call__(self, text) -> bool:
            return self.search(text) is not None

        def search(self, text):
            return self.regex.search(str(text or ""))

    def build_matcher(query: str) -> tuple["QueryMatcher", str]:
        matcher = QueryMatcher(query)
        return matcher, matcher.mode

    # Conversations are the slow half of a search (one transcript read per agent
    # pane), so parsed messages are cached briefly and the scan has a deadline.
    SEARCH_MESSAGE_CACHE: dict[str, tuple[float, list[dict]]] = {}
    SEARCH_MESSAGE_TTL = float(os.environ.get("TMUX_WEB_SEARCH_CACHE_SECONDS", "25"))
    SEARCH_BUDGET = float(os.environ.get("TMUX_WEB_SEARCH_BUDGET_SECONDS", "4"))
    def search_transport(host_id: str):
        """A transport for one host without listing every pane on every host."""
        if host_id in ("", "localhost"):
            return tmux
        try:
            host = remote_hosts.get_host(host_id, include_credential=True)
        except KeyError:
            return None
        return runner_for(host)

    # Conversations are the slow half of a search (one transcript read per agent
    # pane), so parsed messages are cached briefly and the scan has a deadline.
    SEARCH_MESSAGE_CACHE: dict[str, tuple[float, list[dict]]] = {}
    SEARCH_MESSAGE_TTL = float(os.environ.get("TMUX_WEB_SEARCH_CACHE_SECONDS", "25"))
    SEARCH_BUDGET = float(os.environ.get("TMUX_WEB_SEARCH_BUDGET_SECONDS", "4"))
    def cached_transcript_messages(state: dict, transport, deadline: float) -> list[dict] | None:
        pane_id = state["id"]
        now = time.time()
        hit = SEARCH_MESSAGE_CACHE.get(pane_id)
        if hit and (now - hit[0]) < SEARCH_MESSAGE_TTL:
            return hit[1]
        if now > deadline or transport is None:
            return None
        try:
            data = transport.transcript(state.get("pane_id") or pane_id, 120)
        except (RuntimeError, OSError, ValueError, transcript_reader.TranscriptError):
            return None
        messages = data.get("messages") or []
        SEARCH_MESSAGE_CACHE[pane_id] = (now, messages)
        if len(SEARCH_MESSAGE_CACHE) > 80:  # bounded: drop the oldest entry
            oldest = min(SEARCH_MESSAGE_CACHE, key=lambda key: SEARCH_MESSAGE_CACHE[key][0])
            SEARCH_MESSAGE_CACHE.pop(oldest, None)
        return messages

    @app.get("/api/home/search")
    async def home_search(request: Request, q: str = "", limit: int = 20, messages: int = 1):
        """Search hosts, sessions and panes first, then the cached conversations.

        The query is used as a case-insensitive regular expression when it
        compiles; otherwise it is treated as a literal substring.
        """
        require_auth(request)
        query = q.strip()
        limit = max(1, min(int(limit), 60))
        if not query:
            return {"query": query, "mode": "empty", "hosts": [], "sessions": [],
                    "panes": [], "messages": [], "cards": []}

        matcher, mode = build_matcher(query)
        scan_messages = bool(messages)

        def scan() -> dict:
            # the board polls the sweep every 15s; wait only a moment on a cold start
            states = pane_states_cached(2.5, 60.0)
            host_names = {host["id"]: host.get("name") or host["id"]
                          for host in remote_hosts.list_hosts()}
            # 1) servers: name, hostname, id
            hosts_hit = []
            for host in remote_hosts.list_hosts():
                haystack = " ".join(str(host.get(key) or "") for key in ("name", "hostname", "id", "username"))
                if matcher(haystack):
                    hosts_hit.append({"id": host["id"], "name": host.get("name") or host["id"],
                                      "hostname": host.get("hostname") or "",
                                      "port": host.get("port"), "username": host.get("username") or "",
                                      "panes": 0})
            counts = {}
            for state in states:
                counts[state.get("host_id")] = counts.get(state.get("host_id"), 0) + 1
            for host in hosts_hit:
                host["panes"] = counts.get(host["id"], 0)
            if matcher("localhost") and not any(host["id"] == "localhost" for host in hosts_hit):
                hosts_hit.append({"id": "localhost", "name": "localhost", "hostname": "localhost",
                                  "port": 22, "username": "", "panes": counts.get("localhost", 0)})

            # 2) tmux sessions, then 3) panes
            sessions: list[dict] = []
            seen_sessions: set[tuple[str, str]] = set()
            panes: list[dict] = []
            for state in states:
                session = str(state.get("session") or "")
                host_name = state.get("host_name") or host_names.get(state.get("host_id"), "")
                key = (str(state.get("host_id") or ""), session)
                if session and key not in seen_sessions and matcher(session):
                    seen_sessions.add(key)
                    sessions.append({"host": host_name, "host_id": state.get("host_id"),
                                     "session": session,
                                     "panes": sum(1 for other in states
                                                  if other.get("session") == session
                                                  and other.get("host_id") == state.get("host_id"))})
                name_hit = matcher(" ".join(str(state.get(key_) or "") for key_ in
                                            ("title", "target", "provider", "id", "window")))
                path_hit = matcher(str(state.get("path") or ""))
                if name_hit or path_hit:
                    panes.append({**{key_: state.get(key_) for key_ in
                                     ("id", "host_id", "host_name", "target", "session", "provider",
                                      "status", "title", "path", "window")},
                                  "match": "name" if name_hit else "path"})

            # 4) conversations, from the message cache where possible. Transcript
            # reads are the slow part (one SSH round trip per agent pane), so they
            # run concurrently under one deadline and return whatever finished.
            messages: list[dict] = []
            partial = False
            deadline = time.time() + SEARCH_BUDGET
            if not scan_messages:
                # the UI asks for names first (instant) and for messages second
                return {"hosts": hosts_hit[:limit], "sessions": sessions[:limit],
                        "panes": panes[:limit], "messages": [], "cards": [],
                        "messages_partial": True, "messages_pending": True,
                        "indexed": len(states), "indexing": len(states) == 0,
                        "mode": mode}
            transports: dict[str, object] = {}
            candidates = [state for state in states if state.get("provider")][:16]
            for state in candidates:
                host_id = str(state.get("host_id") or "")
                if host_id not in transports:
                    transports[host_id] = search_transport(host_id)
            collected: list[tuple[dict, list[dict]]] = []
            if candidates:
                # no context manager: it would wait for a slow SSH read to finish.
                # Whatever is still running is abandoned and reported as partial.
                pool = ThreadPoolExecutor(max_workers=8)
                futures = {pool.submit(cached_transcript_messages, state,
                                       transports.get(str(state.get("host_id") or "")), deadline): state
                           for state in candidates}
                try:
                    for future in as_completed(futures, timeout=max(0.2, deadline - time.time())):
                        state = futures[future]
                        try:
                            data = future.result()
                        except Exception:  # noqa: BLE001 - one pane must not fail the search
                            continue
                        if data:
                            collected.append((state, data))
                        if len(collected) >= len(candidates):
                            break
                except FuturesTimeout:
                    partial = True
                for future in futures:
                    if not future.done():
                        partial = True
                    future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
            for state, cached_messages in collected:
                if len(messages) >= limit:
                    break
                for message in reversed(cached_messages):
                    text = " ".join((message.get("text") or "").split())
                    found = matcher.search(text)
                    if not found:
                        continue
                    start = max(0, found.start() - 60)
                    snippet = text[start:start + 220]
                    messages.append({
                        "pane": state["id"], "title": state.get("title") or "",
                        "session": state.get("session") or "",
                        "host": state.get("host_name") or "",
                        "provider": state.get("provider"), "role": message.get("role"),
                        "snippet": ("…" if start else "") + snippet +
                                   ("…" if start + 220 < len(text) else ""),
                        "timestamp": message.get("timestamp"),
                    })
                    if len(messages) >= limit:
                        break

            # 5) board cards
            cards = [card for card in home.cards(200)
                     if matcher(f"{card['title']} {card['body']}")]
            return {"hosts": hosts_hit[:limit], "sessions": sessions[:limit],
                    "panes": panes[:limit], "messages": messages, "cards": cards[:limit],
                    "messages_partial": partial, "indexed": len(states),
                    "indexing": len(states) == 0}

        found = await asyncio.to_thread(scan)
        return {"query": query, "mode": mode, **found}

    @app.get("/api/home/cards")
    async def list_home_cards(request: Request, limit: int = 60):
        require_auth(request)
        return {"cards": await asyncio.to_thread(home.cards, limit)}

    @app.post("/api/home/cards", status_code=201)
    async def create_home_card(request: Request, payload: HomeCardRequest):
        require_auth(request)
        try:
            card = await asyncio.to_thread(home.add_card, payload.title, payload.body, payload.pane, payload.kind)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return card

    @app.delete("/api/home/cards/{card_id}")
    async def delete_home_card(request: Request, card_id: str):
        require_auth(request)
        removed = await asyncio.to_thread(home.delete_card, card_id)
        if not removed:
            raise HTTPException(status_code=404, detail="card not found")
        return {"id": card_id, "removed": True}

    @app.get("/api/panes/status")
    async def pane_states(request: Request):
        require_auth(request)
        states = await asyncio.to_thread(pane_states_cached, 2.0, 30.0)
        return {"panes": [{key: item.get(key) for key in ("id", "provider", "status", "status_source")}
                          for item in states]}

    async def perform_relay_ask(payload: RelayAskRequest) -> dict:
        """Ask a pane and wait for the answer: used by the HTTP route and the MCP endpoint."""
        transport, pane_id, pane, host_name = await asyncio.to_thread(relay_target, payload.target)
        provider = transcript_reader.provider_for_command(pane.get("command"))
        lock = relay_locks.setdefault(payload.target, asyncio.Lock())
        if lock.locked():
            raise HTTPException(status_code=409, detail="another relay request is already waiting on this pane")
        started = time.monotonic()
        async with lock:
            try:
                result = await asyncio.to_thread(
                    relay.ask, transport, pane_id, payload.question,
                    source=payload.source, provider=provider,
                    timeout=payload.timeout, force=payload.force,
                )
            except relay.RelayBusy as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except relay.RelayTimeout as exc:
                relay_log.appendleft({"at": time.time(), "target": payload.target, "source": payload.source,
                                      "status": "timeout", "question": payload.question[:400],
                                      "elapsed": round(time.monotonic() - started, 2)})
                raise RelayTimeoutError(str(exc), exc.tail) from exc
            except (relay.RelayError, TmuxError, RuntimeError, OSError, ValueError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        result.update({"target": payload.target, "host": host_name, "provider": provider})
        relay_log.appendleft({"at": time.time(), "target": payload.target, "source": payload.source,
                              "status": "ok", "question": payload.question[:400],
                              "reply": str(result["reply"])[:400], "elapsed": result["elapsed"]})
        return result

    @app.post("/api/relay/ask")
    async def relay_ask(request: Request, payload: RelayAskRequest):
        require_auth(request)
        try:
            return await perform_relay_ask(payload)
        except RelayTimeoutError as exc:
            return JSONResponse({"detail": str(exc), "tail": exc.tail, "target": payload.target},
                                status_code=504)

    @app.post("/api/relay/message")
    async def relay_message(request: Request, payload: RelayMessageRequest):
        require_auth(request)
        transport, pane_id, pane, host_name = await asyncio.to_thread(relay_target, payload.target)
        try:
            await asyncio.to_thread(transport.send, pane_id, payload.text, True)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        relay_log.appendleft({"at": time.time(), "target": payload.target, "source": "message",
                              "status": "sent", "question": payload.text[:400], "elapsed": 0})
        return {"status": "sent", "target": payload.target, "host": host_name}

    @app.get("/api/relay/log")
    async def relay_history(request: Request):
        require_auth(request)
        return {"events": list(relay_log)}

    @app.websocket("/ws/panes/{pane_id}")
    async def stream_pane(websocket: WebSocket, pane_id: str):
        token = websocket.cookies.get(COOKIE_NAME, "")
        if not hmac.compare_digest(token, expected_token()):
            await websocket.close(code=4401)
            return
        try:
            pane_or_404(pane_id)
            transport, actual_pane_id = pane_transport(pane_id)
        except HTTPException:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        # Poll the cheap digest first and only transfer the pane text when it
        # actually changed; idle panes then cost a few bytes per tick, and the
        # interval backs off so an idle tab stops hammering the host.
        digest: str | None = None
        interval = 0.7
        lines = stream_first_lines
        try:
            while True:
                snapshot = getattr(transport, "snapshot_ansi", None)
                if snapshot is None:
                    capture_ansi = getattr(transport, "capture_ansi", transport.capture)
                    content = await asyncio.to_thread(capture_ansi, actual_pane_id, lines)
                    new_digest = hashlib.sha1(content.encode()).hexdigest()
                    payload = None if digest == new_digest else content
                    digest = new_digest
                else:
                    digest, payload = await asyncio.to_thread(snapshot, actual_pane_id, lines, digest)
                # the first frame carries some scrollback, the rest is tail-only
                lines = stream_lines
                if payload is not None:
                    if len(payload) > stream_max_bytes:
                        tail = payload[-stream_max_bytes:]
                        cut = tail.find("\n")
                        dropped = len(payload) - (len(tail) - (cut + 1 if cut >= 0 else 0))
                        payload = f"\x1b[2m… 输出过快，已跳过前面 {dropped} 字节 …\x1b[0m\n" + tail[cut + 1 if cut >= 0 else 0:]
                    await websocket.send_json({"type": "capture", "pane": pane_id, "content": payload,
                                               "lines": lines})
                    interval = 0.7
                else:
                    interval = min(3.0, interval * 1.5)
                await asyncio.sleep(interval)
        except (WebSocketDisconnect, TmuxError, RuntimeError, OSError, ValueError):
            return

    @app.websocket("/ws/terminal/{pane_id}")
    async def terminal_socket(websocket: WebSocket, pane_id: str, cols: int = 100, rows: int = 30):
        token = websocket.cookies.get(COOKIE_NAME, "")
        if not hmac.compare_digest(token, expected_token()):
            await websocket.close(code=4401)
            return
        try:
            pane_or_404(pane_id)
            host_id, actual_pane_id = split_pane_ref(pane_id)
        except (HTTPException, ValueError):
            await websocket.close(code=4404)
            return
        await websocket.accept()
        if host_id == "localhost":
            session = make_terminal(actual_pane_id, cols, rows)
        else:
            try:
                host = remote_hosts.get_host(host_id, include_credential=True)
                session = RemoteTerminalSession(host, actual_pane_id, cols, rows)
            except (KeyError, RuntimeError, OSError, ValueError):
                await websocket.close(code=4503)
                return

        async def send_output():
            while True:
                data = await session.read()
                if not data:
                    return
                await websocket.send_bytes(data)

        async def receive_input():
            try:
                while True:
                    raw = await websocket.receive_text()
                    message = json.loads(raw)
                    if message.get("type") == "input" and isinstance(message.get("data"), str):
                        await session.write(message["data"].encode())
                    elif message.get("type") == "resize":
                        await session.resize(int(message.get("cols", 100)), int(message.get("rows", 30)))
            except WebSocketDisconnect:
                return

        tasks = [asyncio.create_task(send_output()), asyncio.create_task(receive_input())]
        try:
            done, pending = await asyncio.wait(tasks, timeout=900, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (WebSocketDisconnect, json.JSONDecodeError, ValueError, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.shield(session.close())

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


if __name__ == "__main__":
    uvicorn.run(
        "tmux_web:create_app",
        host=os.environ.get("TMUX_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TMUX_WEB_PORT", "8787")),
        factory=True,
        reload=False,
    )
