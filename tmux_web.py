from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import fcntl
import re
import secrets
import struct
import subprocess
import termios
import sqlite3
import time
import threading
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from remote_hosts import RemoteHostStore, SSHRunner, RemoteTerminalSession, split_pane_ref, scan_host_key, host_key_fingerprint, validate_private_key_text

PANE_RE = re.compile(r"^%[0-9]+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
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
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            remainder = b""
            while position > 0:
                size = min(chunk_size, position)
                position -= size
                handle.seek(position)
                block = handle.read(size) + remainder
                parts = block.split(b"\n")
                remainder = parts[0]
                if len(remainder) > max_line:
                    remainder = b""
                for raw in reversed(parts[1:]):
                    if raw and len(raw) <= max_line:
                        yield raw.decode(errors="replace")
            if remainder and len(remainder) <= max_line:
                yield remainder.decode(errors="replace")

    @staticmethod
    def _file_contains(path: Path, needle: str) -> bool:
        target = needle.encode()
        overlap = max(0, len(target) - 1)
        previous = b""
        with path.open("rb") as handle:
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    return False
                data = previous + block
                if target in data:
                    return True
                previous = data[-overlap:] if overlap else b""

    @staticmethod
    def _run(*args: str, input_bytes: bytes | None = None) -> str:
        completed = subprocess.run(
            ["tmux", *args], input=input_bytes, capture_output=True, check=False
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            raise TmuxError(detail or "tmux command failed")
        return completed.stdout.decode(errors="replace")

    def list_panes(self) -> list[dict]:
        format_string = "\t".join([
            "#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}",
            "#{session_name}", "#{window_index}", "#{window_name}", "#{pane_index}",
            "#{pane_current_command}", "#{pane_title}", "#{pane_current_path}",
            "#{pane_active}", "#{pane_width}", "#{pane_height}",
        ])
        raw = self._run("list-panes", "-a", "-F", format_string)
        panes = []
        for line in raw.splitlines():
            fields = line.split("\t")
            if len(fields) != 12:
                continue
            pane_id, target, session, window, window_name, index, command, title, path, active, width, height = fields
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

    def capture(self, pane_id: str, history: int = 500) -> str:
        self.validate_pane(pane_id)
        history = max(20, min(history, 5000))
        content = self._run("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{history}")
        return ANSI_RE.sub("", content)

    def capture_ansi(self, pane_id: str, history: int = 500) -> str:
        self.validate_pane(pane_id)
        history = max(20, min(history, 5000))
        return self._run("capture-pane", "-p", "-J", "-e", "-t", pane_id, "-S", f"-{history}")

    def _pane_session_id(self, pane_id: str) -> str:
        pane = self.validate_pane(pane_id)
        if pane.get("command") != "claude":
            raise TmuxError("pane is not running Claude Code")
        root_pid = int(self._run("display-message", "-p", "-t", pane_id, "#{pane_pid}").strip())
        queue = [root_pid]
        seen = set()
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                command = ""
            match = re.search(r"(?:--resume|-r)\s+([0-9a-f-]{36})", command)
            if match:
                return match.group(1)
            children = subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
            ).stdout.split()
            queue.extend(int(child) for child in children)

        project_dir = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", pane.get("path", ""))
        candidates = list(project_dir.glob("*.jsonl")) if project_dir.is_dir() else []
        title = re.sub(r"^[^A-Za-z0-9\u4e00-\u9fff]+", "", pane.get("title", "")).strip()
        if title:
            title_matches = []
            for path in candidates:
                try:
                    if self._file_contains(path, title):
                        title_matches.append(path)
                except OSError:
                    continue
            if len(title_matches) == 1:
                return title_matches[0].stem
        try:
            screen = ANSI_RE.sub("", self.capture_ansi(pane_id, 80))
        except TmuxError:
            screen = ""
        probes = [line.strip() for line in screen.splitlines() if len(line.strip()) >= 24]
        for probe in reversed(probes):
            matches = []
            for path in candidates:
                try:
                    if self._file_contains(path, probe):
                        matches.append(path)
                except OSError:
                    continue
            if len(matches) == 1:
                return matches[0].stem
        raise TmuxError("could not determine Claude session id")

    @staticmethod
    def _content_text(content: object, allowed_types: set[str]) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in allowed_types:
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _claude_transcript(self, pane_id: str, limit: int) -> dict:
        session_id = self._pane_session_id(pane_id)
        matches = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
        if not matches:
            raise TmuxError("Claude transcript not found")
        max_records = max(1, min(limit, 1000))
        records: list[dict] = []
        seen: set[str] = set()
        for line in self._reverse_lines(matches[0]):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = event.get("type")
            uuid = event.get("uuid")
            if event.get("isCompactSummary"):
                continue
            if role not in {"user", "assistant"} or not uuid or uuid in seen:
                continue
            message = event.get("message") or {}
            text = self._content_text(message.get("content"), {"text"})
            if not text or text.startswith("<task-notification>") or text.startswith("<local-command-"):
                continue
            seen.add(uuid)
            records.append({"id": uuid, "role": role, "text": text, "timestamp": event.get("timestamp")})
            if len(records) >= max_records:
                break
        records.reverse()
        return {
            "pane": pane_id,
            "session_id": session_id,
            "messages": records,
            "mtime": matches[0].stat().st_mtime_ns,
            "provider": "claude",
        }

    def _codex_session_file(self, pane: dict) -> tuple[str, Path]:
        codex_home = Path.home() / ".codex"
        title = pane.get("title", "").split(" | ", 1)[0].strip()
        session_id = None
        index_path = codex_home / "session_index.jsonl"
        if index_path.exists() and title:
            for line in index_path.read_text(errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("thread_name") == title:
                    session_id = entry.get("id")
        files = list((codex_home / "sessions").rglob("*.jsonl"))
        if session_id:
            candidates = [path for path in files if session_id in path.name]
            if candidates:
                return session_id, max(candidates, key=lambda path: path.stat().st_mtime_ns)
        cwd = pane.get("path")
        candidates = []
        for path in files:
            try:
                first = json.loads(path.open(errors="replace").readline())
            except (OSError, json.JSONDecodeError):
                continue
            payload = first.get("payload") or {}
            if first.get("type") == "session_meta" and payload.get("cwd") == cwd:
                candidates.append((path, payload.get("id") or payload.get("session_id")))
        if candidates:
            path, found_id = max(candidates, key=lambda item: item[0].stat().st_mtime_ns)
            return str(found_id), path
        raise TmuxError("Codex transcript not found")

    def _codex_transcript(self, pane_id: str, limit: int) -> dict:
        pane = self.validate_pane(pane_id)
        session_id, path = self._codex_session_file(pane)
        records = []
        seen = set()
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "response_item":
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
                    continue
                message_id = payload.get("id")
                if not message_id or message_id in seen:
                    continue
                role = payload["role"]
                text = self._content_text(payload.get("content"), {"input_text", "output_text", "text"})
                if not text or text.startswith(("<environment_context>", "<skills_instructions>", "<permissions instructions>", "<model_switch>", "<collaboration_mode>")):
                    continue
                seen.add(message_id)
                records.append({"id": message_id, "role": role, "text": text, "timestamp": event.get("timestamp")})
        return {"pane": pane_id, "provider": "codex", "session_id": session_id, "messages": records[-max(1, min(limit, 1000)):], "mtime": path.stat().st_mtime_ns}

    def _hermes_transcript(self, pane_id: str, limit: int) -> dict:
        pane = self.validate_pane(pane_id)
        db_path = Path.home() / ".hermes" / "state.db"
        if not db_path.exists():
            raise TmuxError("Hermes state database not found")
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            session = connection.execute(
                "SELECT id FROM sessions WHERE source = 'cli' AND cwd = ? ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1",
                (pane.get("path"),),
            ).fetchone()
            if session is None:
                raise TmuxError("Hermes CLI session not found")
            session_id = session["id"]
            rows = connection.execute(
                "SELECT id, role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 AND role IN ('user','assistant') AND COALESCE(content,'') <> '' ORDER BY id DESC LIMIT ?",
                (session_id, max(1, min(limit, 1000))),
            ).fetchall()
        messages = [
            {"id": str(row["id"]), "role": row["role"], "text": row["content"], "timestamp": row["timestamp"]}
            for row in reversed(rows)
        ]
        return {"pane": pane_id, "provider": "hermes", "session_id": session_id, "messages": messages, "mtime": db_path.stat().st_mtime_ns}

    def transcript(self, pane_id: str, limit: int = 200) -> dict:
        pane = self.validate_pane(pane_id)
        command = pane.get("command")
        if command == "claude":
            return self._claude_transcript(pane_id, limit)
        if command == "codex":
            return self._codex_transcript(pane_id, limit)
        if command == "python3":
            return self._hermes_transcript(pane_id, limit)
        raise TmuxError("structured transcript is not supported for this pane")

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


class ShareCreateRequest(BaseModel):
    pane_id: str
    slug: str = Field(min_length=3, max_length=48)
    password: str = Field(default="", max_length=256)
    expires_in: int = Field(ge=300, le=60 * 60 * 24 * 90)


class ShareExtendRequest(BaseModel):
    seconds: int = Field(ge=300, le=60 * 60 * 24 * 90)


class SharePasswordRequest(BaseModel):
    password: str = Field(default="", max_length=256)


class RemoteHostCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    auth_type: str
    credential: str = Field(min_length=1, max_length=100_000)
    host_key: str = Field(min_length=20, max_length=20_000)
    fingerprint: str = Field(min_length=8, max_length=256)
    confirm_fingerprint: str = Field(min_length=8, max_length=256)


class TerminalSession:
    def __init__(self, pane_id: str, cols: int, rows: int):
        self.pane_id = pane_id
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

    def _set_size(self, cols: int, rows: int) -> None:
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 300))
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

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


def create_app(
    backend: TmuxBackend | None = None,
    *,
    password: str | None = None,
    session_secret: str | None = None,
    terminal_factory=None,
    share_db: str | None = None,
    remote_store=None,
    remote_runner_factory=None,
) -> FastAPI:
    tmux = backend or TmuxBackend()
    configured_password = password if password is not None else os.environ.get("TMUX_WEB_PASSWORD")
    if not configured_password:
        raise RuntimeError("TMUX_WEB_PASSWORD must be set")
    secret = session_secret or os.environ.get("TMUX_WEB_SESSION_SECRET") or secrets.token_hex(32)
    expected_token = session_token(configured_password, secret)
    make_terminal = terminal_factory or TerminalSession
    remote_hosts = remote_store or RemoteHostStore(
        os.environ.get("TMUX_WEB_HOST_DB", str(BASE_DIR / "remote_hosts.db")),
        os.environ.get("TMUX_WEB_HOST_KEY", str(BASE_DIR / ".remote_hosts.key")),
    )
    make_remote_runner = remote_runner_factory or SSHRunner
    share_db_path = share_db or os.environ.get("TMUX_WEB_SHARE_DB") or str(BASE_DIR / "shares.db")
    share_connection = sqlite3.connect(share_db_path, check_same_thread=False)
    share_connection.row_factory = sqlite3.Row
    share_lock = threading.Lock()
    login_limiter = LoginRateLimiter()
    with share_connection:
        share_connection.execute("""
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
                revoked_at REAL
            )
        """)

    app = FastAPI(title="Tmux Web Console", docs_url=None, redoc_url=None, openapi_url=None)

    def login_key(request: Request, scope: str) -> str:
        client = request.client.host if request.client else "unknown"
        return f"{scope}:{client}"

    def secure_cookie(request: Request) -> bool:
        setting = os.environ.get("TMUX_WEB_SECURE_COOKIES", "auto").strip().lower()
        if setting in {"1", "true", "yes", "on"}:
            return True
        if setting in {"0", "false", "no", "off"}:
            return False
        return request.url.scheme == "https"

    def authenticated(request: Request) -> bool:
        token = request.cookies.get(COOKIE_NAME, "")
        return hmac.compare_digest(token, expected_token)

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
                panes = make_remote_runner(host).list_panes()
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
        return make_remote_runner(host), pane_id

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
        try:
            pane = tmux.validate_pane(row["pane_id"])
            data = tmux.transcript(row["pane_id"], 1)
        except (TmuxError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail="shared session is unavailable") from exc
        if str(data.get("session_id")) != row["session_id"]:
            raise HTTPException(status_code=409, detail="shared pane no longer points to the original session")
        return pane

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if authenticated(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(WEB_DIR / "login.html")

    @app.post("/login")
    async def login(request: Request):
        rate_key = login_key(request, "admin")
        login_limiter.check(rate_key)
        body = (await request.body()).decode(errors="replace")
        supplied = parse_qs(body).get("password", [""])[0]
        if not hmac.compare_digest(supplied, configured_password):
            login_limiter.fail(rate_key)
            html = (WEB_DIR / "login.html").read_text().replace(
                "<!--ERROR-->", '<p class="error" role="alert">密码错误</p>'
            )
            return HTMLResponse(html, status_code=401)
        login_limiter.success(rate_key)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            expected_token,
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
            return RedirectResponse("/login", status_code=303)
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/shares")
    async def shares_page(request: Request):
        if not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(WEB_DIR / "shares.html", headers={"Cache-Control": "no-store"})

    @app.get("/hosts")
    async def hosts_page(request: Request):
        if not authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(WEB_DIR / "hosts.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/shares", status_code=201)
    async def create_share(request: Request, payload: ShareCreateRequest):
        require_auth(request)
        if not SHARE_SLUG_RE.fullmatch(payload.slug):
            raise HTTPException(status_code=422, detail="slug must use lowercase letters, numbers, and hyphens")
        pane = pane_or_404(payload.pane_id)
        if pane.get("command") != "claude":
            raise HTTPException(status_code=422, detail="only Claude transcript sessions can be shared")
        try:
            transcript_data = await asyncio.to_thread(tmux.transcript, payload.pane_id, 1)
        except TmuxError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        now = time.time()
        share_id = secrets.token_hex(16)
        password_hash = hash_share_password(payload.password) if payload.password else None
        try:
            with share_lock, share_connection:
                share_connection.execute(
                    "INSERT INTO shares VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (share_id, payload.slug, payload.pane_id, transcript_data.get("provider", "claude"),
                     str(transcript_data["session_id"]), pane.get("title") or pane.get("target") or "Claude",
                     password_hash, now, now + payload.expires_in),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="share slug already exists") from exc
        return {"id": share_id, "slug": payload.slug, "url": f"/share/{payload.slug}",
                "expires_at": now + payload.expires_in, "password_protected": bool(password_hash)}

    @app.get("/api/shares")
    async def list_shares(request: Request):
        require_auth(request)
        with share_lock:
            rows = share_connection.execute(
                "SELECT id,slug,pane_id,provider,session_id,title,created_at,expires_at,revoked_at,password_hash FROM shares ORDER BY created_at DESC"
            ).fetchall()
        now = time.time()
        return {"shares": [{
            "id": row["id"], "slug": row["slug"], "url": f"/share/{row['slug']}",
            "pane_id": row["pane_id"], "provider": row["provider"], "session_id": row["session_id"],
            "title": row["title"], "created_at": row["created_at"], "expires_at": row["expires_at"],
            "revoked": row["revoked_at"] is not None, "password_protected": bool(row["password_hash"]),
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
        ).replace("__SHARE_UNLOCKED__", "true" if unlocked else "false")
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

    @app.get("/share/{slug}/api/transcript")
    async def share_transcript(request: Request, slug: str, limit: int = 300):
        row = get_share(slug)
        require_share_access(request, row)
        validate_shared_session(row)
        return await asyncio.to_thread(tmux.transcript, row["pane_id"], limit)

    @app.post("/share/{slug}/api/input")
    async def share_input(request: Request, slug: str, payload: InputRequest):
        row = get_share(slug)
        require_share_access(request, row)
        validate_shared_session(row)
        await asyncio.to_thread(tmux.send, row["pane_id"], payload.text, payload.submit)
        return {"status": "sent", "submit": payload.submit}

    @app.get("/manifest.webmanifest")
    async def web_manifest(request: Request):
        require_auth(request)
        return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker(request: Request):
        require_auth(request)
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="text/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/assets/app-icon.svg")
    async def app_icon_svg(request: Request):
        require_auth(request)
        return FileResponse(WEB_DIR / "app-icon.svg", media_type="image/svg+xml")

    @app.get("/assets/app-icon-{size}.png")
    async def app_icon_png(request: Request, size: int):
        require_auth(request)
        if size not in {180, 192, 512}:
            raise HTTPException(status_code=404, detail="icon not found")
        return FileResponse(WEB_DIR / f"app-icon-{size}.png", media_type="image/png")

    @app.get("/assets/xterm/{filename}")
    async def xterm_asset(request: Request, filename: str):
        require_auth(request)
        if filename not in {"xterm.css", "xterm.js", "addon-fit.js"}:
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(WEB_DIR / "vendor" / "xterm" / filename)

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
            return {"panes": tmux.list_panes()}
        except TmuxError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/hosts")
    async def list_hosts(request: Request):
        require_auth(request)
        hosts = []
        try:
            panes = tmux.list_panes()
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
        except TmuxError as exc:
            hosts.append({"id": "localhost", "name": "localhost", "kind": "local",
                          "status": "offline", "error": str(exc), "panes": []})
        for remote in remote_hosts.list_hosts():
            public = {key: remote[key] for key in ("id", "name", "hostname", "port", "username", "auth_type", "fingerprint")}
            public.update({"kind": "ssh", "status": "offline", "panes": []})
            try:
                credentialed = remote_hosts.get_host(remote["id"], include_credential=True)
                public["panes"] = await asyncio.to_thread(make_remote_runner(credentialed).list_panes)
                public["status"] = "online"
            except (RuntimeError, OSError, KeyError) as exc:
                public["error"] = str(exc)
            hosts.append(public)
        return {"hosts": hosts}

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

    @app.post("/api/remote-hosts", status_code=201)
    async def create_remote_host(request: Request, payload: RemoteHostCreateRequest):
        require_auth(request)
        if payload.auth_type not in {"password", "key"}:
            raise HTTPException(status_code=422, detail="auth_type must be password or key")
        if payload.auth_type == "key":
            try:
                validate_private_key_text(payload.credential)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            computed = host_key_fingerprint(payload.host_key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not hmac.compare_digest(computed, payload.fingerprint) or not hmac.compare_digest(
            payload.fingerprint, payload.confirm_fingerprint
        ):
            raise HTTPException(status_code=409, detail="SSH host key fingerprint was not confirmed")
        host = remote_hosts.add_host(
            name=payload.name, hostname=payload.hostname, port=payload.port, username=payload.username,
            auth_type=payload.auth_type, credential=payload.credential, host_key=payload.host_key,
            fingerprint=payload.fingerprint,
        )
        try:
            credentialed = remote_hosts.get_host(host["id"], include_credential=True)
            await asyncio.to_thread(make_remote_runner(credentialed).check)
        except (RuntimeError, OSError) as exc:
            remote_hosts.delete_host(host["id"])
            raise HTTPException(status_code=424, detail=str(exc)) from exc
        return host

    @app.post("/api/remote-hosts/test")
    async def test_remote_host(request: Request, payload: RemoteHostCreateRequest):
        require_auth(request)
        if payload.auth_type not in {"password", "key"}:
            raise HTTPException(status_code=422, detail="auth_type must be password or key")
        if payload.auth_type == "key":
            try:
                validate_private_key_text(payload.credential)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    @app.get("/api/remote-hosts")
    async def list_remote_hosts(request: Request):
        require_auth(request)
        return {"hosts": [{key: host[key] for key in (
            "id", "name", "hostname", "port", "username", "auth_type", "fingerprint", "created_at", "enabled"
        )} for host in remote_hosts.list_hosts()]}

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

    @app.get("/api/panes/{pane_id}/transcript")
    async def transcript(request: Request, pane_id: str, limit: int = 200):
        require_auth(request)
        pane = pane_or_404(pane_id)
        if pane.get("host_id", "localhost") != "localhost":
            raise HTTPException(status_code=409, detail="structured transcript is not available for remote hosts yet")
        try:
            return await asyncio.to_thread(tmux.transcript, pane_id, limit)
        except TmuxError as exc:
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


    @app.websocket("/ws/panes/{pane_id}")
    async def stream_pane(websocket: WebSocket, pane_id: str):
        token = websocket.cookies.get(COOKIE_NAME, "")
        if not hmac.compare_digest(token, expected_token):
            await websocket.close(code=4401)
            return
        try:
            pane_or_404(pane_id)
            transport, actual_pane_id = pane_transport(pane_id)
        except HTTPException:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        previous: str | None = None
        try:
            while True:
                capture_ansi = getattr(transport, "capture_ansi", transport.capture)
                content = await asyncio.to_thread(capture_ansi, actual_pane_id, 5000)
                if content != previous:
                    await websocket.send_json({"type": "capture", "pane": pane_id, "content": content})
                    previous = content
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, TmuxError, RuntimeError, OSError, ValueError):
            return

    @app.websocket("/ws/terminal/{pane_id}")
    async def terminal_socket(websocket: WebSocket, pane_id: str, cols: int = 100, rows: int = 30):
        token = websocket.cookies.get(COOKIE_NAME, "")
        if not hmac.compare_digest(token, expected_token):
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
