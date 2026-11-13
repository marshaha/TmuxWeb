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
import shlex
import socket
import sys
import urllib.parse
import signal
import struct
import subprocess
import termios
import sqlite3

from auth_store import PasswordStore
import federation
from plugin_api import PluginError, PluginManager, PluginServices, install_routes
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
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field, field_validator
import file_browser
from home_store import HomeStore
import mcp_relay
import agent_status
import agent_catalog
import agent_models
import agent_prompts
import relay
import transcript_reader
import transfer_tickets
from remote_hosts import (AUTH_TYPES, FIELD_SEPARATOR, RemoteHostStore, RemoteTerminalSession, SSHRunner,
                           UploadOffsetMismatch,
                          host_key_fingerprint, normalize_session_name, pane_ref, scan_host_key, split_pane_ref,
                          validate_private_key_text)

LOGGER = logging.getLogger("tmux_web")

PANE_RE = re.compile(r"^%[0-9]+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

# Long polling on the transcript route: a steady-state poll hangs for up to
# this long waiting for the session file (or the agent's running state) to
# move, so one request replaces ~10 polls on a slow link. 25s stays under the
# usual 30s proxy idle timeout; 0 keeps the old poll-stub behaviour.
LONG_POLL_MAX_SECONDS = 25.0
LONG_POLL_CHECK_SECONDS = 0.2
LONG_POLL_WAITING_CHECK_SECONDS = 1.0


def clamp_long_poll_wait(value) -> float:
    """Clamp a client-supplied long-poll wait to 0..LONG_POLL_MAX_SECONDS."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(LONG_POLL_MAX_SECONDS, seconds))
def safe_next_url(value: str, default: str = "/") -> str:
    """Only a same-origin path may be used as a post-login target."""
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if any(char in candidate for char in ("\\", "\r", "\n", "\t")):
        return default
    return candidate


COOKIE_NAME = "tmux_web_session"
THEME_COOKIE = "tmux_theme"      # readable by scripts: pages paint in the account's theme before any fetch
SHARE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"


# The console's scripts, concatenated once at startup into one bundle: one
# request instead of sixteen, which matters most where a round trip costs a
# third of a second or more. Every file is an IIFE exporting a single
# window.Tmux* namespace, so concatenation cannot collide; files are joined
# with ";\n" so a missing trailing semicolon can never merge two files into
# one call, and sourceMappingURL lines are dropped (they would 404 under the
# bundle URL). theme.js stays separate: it must run synchronously before
# first paint. The order below is the order the tags used to load in.
CONSOLE_BUNDLE_SOURCES = (
    ("assets", "uploads.js"),
    ("assets", "composer-files.js"),
    ("assets", "path-links.js"),
    ("assets", "prompt-card.js"),
    ("assets", "markdown-cjk.js"),
    ("assets", "video-cards.js"),
    ("assets", "upload-cards.js"),
    ("assets", "transfer.js"),
    ("assets", "downloads.js"),
    ("assets", "pane-preview.js"),
    ("vendor/xterm", "xterm.js"),
    ("vendor/xterm", "addon-fit.js"),
    ("assets", "plugins-ui.js"),
    ("assets", "recents.js"),
    ("assets", "settings.js"),
)


def build_console_bundle() -> tuple[bytes, str]:
    """Concatenate the console scripts once; return (bytes, 12-char sha1).

    Bytes are read raw and split on "\\n" only: str.splitlines() would also
    split on the control bytes xterm.js legitimately carries inside string
    literals (\\x1c/\\x1d/\\x1e/\\x85), corrupting them into newlines.
    """
    parts = []
    for directory, name in CONSOLE_BUNDLE_SOURCES:
        text = (WEB_DIR / directory / name).read_bytes().decode("utf-8")
        lines = [line for line in text.split("\n")
                 if not line.startswith("//# sourceMappingURL=")]
        parts.append("/* console.bundle: %s/%s */\n%s" % (directory, name, "\n".join(lines)))
    blob = ";\n".join(parts).encode("utf-8")
    return blob, hashlib.sha1(blob).hexdigest()[:12]


CONSOLE_BUNDLE_BYTES, CONSOLE_BUNDLE_HASH = build_console_bundle()


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

    def create_session(self, name: str, path: str = "", command: list[str] | None = None,
                       create_dir: bool = True, keep_open: bool = True) -> dict:
        try:
            name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        directory = str(path or "").strip()
        made_directory = False
        if directory:
            resolved = Path(directory).expanduser()
            if not resolved.is_dir():
                if not create_dir:
                    raise TmuxError("no such directory: %s" % directory)
                if resolved.exists():
                    raise TmuxError("not a directory: %s" % directory)
                # A fresh session usually means a fresh project directory; make
                # it instead of making the user go and do it by hand.
                try:
                    resolved.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise TmuxError("cannot create directory %s: %s" % (directory, exc)) from exc
                made_directory = True
            directory = str(resolved)
        args = ["new-session", "-d", "-s", name]
        if directory:
            args += ["-c", directory]
        if command:
            args += [agent_catalog.with_keep_open(command) if keep_open else agent_catalog.render(command)]
        try:
            self._run(*args)
        except TmuxError as exc:
            if "duplicate session" in str(exc):
                raise TmuxError("session already exists: %s" % name) from exc
            raise
        return {"session": name, "path": directory, "created_dir": made_directory,
                "command": agent_catalog.render(command) if command else ""}

    def kill_pane(self, pane_id: str) -> dict:
        """Close one pane; the session survives if it has other panes."""
        pane = self.validate_pane(pane_id)
        session = pane.get("session") or ""
        try:
            self._run("kill-pane", "-t", pane_id)
        except TmuxError as exc:
            if "no such pane" in str(exc) or "can't find pane" in str(exc):
                raise TmuxError("no such pane: %s" % pane_id) from exc
            raise
        alive = True
        if session:
            try:
                self._run("has-session", "-t", session)
            except TmuxError:
                alive = False
        return {"pane": pane_id, "session": session, "session_alive": alive}

    def kill_session(self, name: str) -> dict:
        """Close a session (and every window/pane in it)."""
        try:
            name = normalize_session_name(name)
        except ValueError as exc:
            raise TmuxError(str(exc)) from exc
        try:
            self._run("kill-session", "-t", name)
        except TmuxError as exc:
            detail = str(exc)
            if "can't find session" in detail or "no such session" in detail:
                raise TmuxError("no such session: %s" % name) from exc
            raise
        return {"session": name, "killed": True}

    def list_panes(self) -> list[dict]:
        format_string = FIELD_SEPARATOR.join([
            "#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}",
            "#{session_name}", "#{window_index}", "#{window_name}", "#{pane_index}",
            "#{pane_current_command}", "#{pane_title}", "#{pane_current_path}",
            "#{pane_active}", "#{pane_width}", "#{pane_height}", "#{window_activity}",
            "#{pane_pid}",
        ])
        raw = self._run("list-panes", "-a", "-F", format_string)
        panes = []
        for line in raw.splitlines():
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) not in (12, 13, 14):
                continue
            pane_id, target, session, window, window_name, index, command, title, path, active, width, height = fields[:12]
            activity = fields[12] if len(fields) > 12 else ""
            pane_pid = fields[13] if len(fields) > 13 else ""
            pane = {
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
            }
            if pane_pid:
                # lets transcript() skip its own display-message lookup;
                # older output without the field keeps the old dict shape
                pane["pane_pid"] = pane_pid
            panes.append(pane)
        table = local_process_table()
        for pane in panes:
            pane["provider"] = transcript_reader.provider_for_pane(pane, table)
        return panes

    def validate_pane(self, pane_id: str) -> dict:
        if not PANE_RE.fullmatch(pane_id):
            raise TmuxError("invalid pane id")
        for pane in self.list_panes():
            if pane["id"] == pane_id:
                return pane
        raise TmuxError("pane not found")

    def capture_many(self, pane_ids, history: int = 40, panes: dict | None = None) -> dict:
        """Local captures are a few ms each; this keeps the sweep's interface uniform.

        ``panes`` maps pane ids to dicts the caller already listed (the sweep's
        own listing): a listed pane skips its own validation, a missing entry
        falls back to it, and no mapping keeps the old behaviour entirely.
        """
        result = {}
        for pane_id in pane_ids:
            try:
                result[pane_id] = self.capture(pane_id, history, pane=(panes or {}).get(pane_id))
            except (RuntimeError, OSError, ValueError, TmuxError):
                continue
        return result

    def capture(self, pane_id: str, history: int = 500, pane: dict | None = None,
                visible_only: bool = False) -> str:
        # routes that already validated (pane_or_404) pass the pane through so
        # one request pays for one list, not two; direct callers validate here
        if pane is None:
            self.validate_pane(pane_id)
        history = max(20, min(history, 5000))
        if visible_only:
            # 只抓可见屏：等待输入的菜单一定在屏幕上，历史里的旧帧只会误导解析
            content = self._run("capture-pane", "-p", "-J", "-t", pane_id)
        else:
            content = self._run("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{history}")
        return ANSI_RE.sub("", content)

    def capture_ansi(self, pane_id: str, history: int = 500, pane: dict | None = None) -> str:
        if pane is None:
            self.validate_pane(pane_id)
        history = max(20, min(history, 5000))
        return self._run("capture-pane", "-p", "-J", "-e", "-t", pane_id, "-S", f"-{history}")

    def pane_pid(self, pane_id: str) -> str:
        return self._run("display-message", "-p", "-t", pane_id, "#{pane_pid}").strip()

    def transcript(self, pane_id: str, limit: int = 200, refresh: bool = False, pane: dict | None = None) -> dict:
        # `refresh` exists so the API can pass it for every transport: remote hosts cache
        # the pulled bytes, the local read is always fresh, so it is accepted and ignored.
        # Discovery and parsing live in transcript_reader so the exact same code
        # produces transcripts here and on SSH remote hosts.
        if pane is None:
            pane = self.validate_pane(pane_id)
        provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
        if provider is None:
            raise TmuxError("structured transcript is not supported for this pane")
        # A resumed CLI session tells us exactly which transcript to read; the
        # local/web gateway does not, so the reader falls back to cwd probing.
        # The pid rides along with the listing now, so a validated pane skips
        # the extra display-message call; direct callers still pay for it.
        # A failing pid lookup must not kill the transcript: without it we just
        # lose the --resume hint and fall back to cwd probing.
        pid = pane.get("pane_pid") or ""
        if not pid:
            try:
                pid = self.pane_pid(pane_id)
            except (TmuxError, RuntimeError, OSError, ValueError):
                pid = ""
        hint = transcript_reader.resume_session_hint(pid) if pid else None
        try:
            data = transcript_reader.read_transcript(
                provider, pane.get("path", ""), pane.get("title", ""), limit,
                home=Path.home(), hint=hint, screen=lambda: self.capture(pane_id, 80, pane=pane),
            )
        except transcript_reader.TranscriptError as exc:
            raise TmuxError(str(exc)) from exc
        data["pane"] = pane_id
        return data

    def snapshot_ansi(self, pane_id: str, history: int = 5000,
                      known_digest: str | None = None, pane: dict | None = None) -> tuple[str, str | None]:
        """Return (digest, content); content is None when nothing changed."""
        content = self.capture_ansi(pane_id, history, pane=pane)
        digest = hashlib.sha1(content.encode()).hexdigest()
        if known_digest is not None and digest == known_digest:
            return digest, None
        return digest, content

    def list_files(self, directory: str, show_hidden: bool = False, limit: int = 2000) -> dict:
        try:
            return file_browser.list_directory(directory, show_hidden=show_hidden, limit=limit)
        except (file_browser.FileBrowserError, OSError, ValueError) as exc:
            raise TmuxError(str(exc)) from exc

    def find_suffix(self, base: str, relative: str, max_depth: int = 4, limit: int = 5) -> list[str]:
        """在 base 下面找以 relative 结尾的路径（agent 常报相对某个子目录的路径）。

        agent 说 ``ate/s3k/note.md``，真实位置可能是 ``<cwd>/runs/195/ate/s3k/note.md``。
        按层走，跳过 .git/node_modules 这类目录，最多 max_depth 层、limit 个结果。
        """
        import os as _os

        relative = (relative or "").strip().lstrip("/")
        if not relative or relative.startswith(".."):
            return []
        root = _os.path.abspath(_os.path.expanduser(base or "."))
        if not _os.path.isdir(root):
            return []
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                ".mypy_cache", ".pytest_cache", "target", ".tox"}
        matches: list[str] = []
        visited = 0
        for current, dirs, _files in _os.walk(root):
            depth = 0 if current == root else _os.path.relpath(current, root).count(_os.sep) + 1
            if depth >= max_depth:
                dirs[:] = []
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in skip)
            visited += 1
            if visited > 4000:
                break
            candidate = _os.path.join(current, relative)
            if _os.path.exists(candidate):
                matches.append(candidate)
                if len(matches) >= limit:
                    break
        return matches

    def download_file(self, path: str, max_bytes: int) -> bytes:
        resolved = file_browser.resolve_path(path)
        if not resolved.is_file():
            raise TmuxError("not a file: %s" % resolved)
        if resolved.stat().st_size > max_bytes:
            raise TmuxError("file is larger than the download limit")
        with resolved.open("rb") as handle:
            return handle.read(max_bytes + 1)

    def file_size(self, path: str) -> int | None:
        """Size of a local file, or None when it is missing/unreadable."""
        try:
            resolved = file_browser.resolve_path(path)
        except (OSError, ValueError):
            return None
        if not resolved.is_file():
            return None
        return resolved.stat().st_size

    def stream_file(self, path: str, max_bytes: int, chunk: int = 65536,
                    idle_timeout: float | None = None):
        """Yield a local file in chunks instead of buffering the whole thing.

        The remote runner already streams (``c680fdd``); the local one still
        read the full file, so a 500MB download over a slow Cloudflare link
        held the whole file in the single worker's memory for minutes and
        could not start sending until the last byte was read.
        """
        resolved = file_browser.resolve_path(path)
        if not resolved.is_file():
            raise TmuxError("not a file: %s" % resolved)
        if resolved.stat().st_size > max_bytes:
            raise TmuxError("file is larger than the download limit")
        # idle_timeout 只对远端有意义（等 ssh 管道），本地读盘不会卡住；
        # 参数存在是为了让调用方对两种 transport 用同一个签名。
        with resolved.open("rb") as handle:
            while True:
                data = handle.read(chunk)
                if not data:
                    break
                yield data

    def zip_files(self, paths: list[str], base: str, max_bytes: int, max_files: int) -> bytes:
        try:
            return file_browser.zip_paths(paths, base, max_bytes=max_bytes, max_files=max_files)
        except (file_browser.FileBrowserError, OSError, ValueError) as exc:
            raise TmuxError(str(exc)) from exc

    def upload_chunk(self, part: str, data: bytes, offset: int) -> int:
        """Append one chunk to a ``.part`` file; returns the size afterwards."""
        resolved = file_browser.resolve_path(part)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if offset:
            size = resolved.stat().st_size if resolved.exists() else 0
            if size != offset:
                raise UploadOffsetMismatch(size)     # 和远端一样：对不上就报当前位置
            with resolved.open("ab") as handle:
                handle.write(data)
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with resolved.open("wb") as handle:
                handle.write(data)
        return resolved.stat().st_size

    def finish_upload(self, part: str, path: str) -> None:
        resolved_part = file_browser.resolve_path(part)
        resolved = file_browser.resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        os.replace(resolved_part, resolved)

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

    def delete_file(self, path: str) -> None:
        resolved = file_browser.resolve_path(path)
        if resolved.is_file() or resolved.is_symlink():
            resolved.unlink()

    def rename_path(self, path: str, new_name: str) -> str:
        try:
            return file_browser.rename_path(path, new_name)
        except (file_browser.FileBrowserError, OSError, ValueError) as exc:
            raise TmuxError(str(exc)) from exc

    def delete_path(self, path: str, recursive: bool = False) -> str:
        try:
            return file_browser.remove_path(path, recursive)
        except (file_browser.FileBrowserError, OSError, ValueError) as exc:
            raise TmuxError(str(exc)) from exc

    def send_key(self, pane_id: str, key: str, pane: dict | None = None) -> None:
        if pane is None:
            self.validate_pane(pane_id)
        self._run("send-keys", "-t", pane_id, key)

    def send_keys(self, pane_id: str, keys: list[str], pane: dict | None = None) -> None:
        """一次把一串键发进去（菜单导航要走好几步，逐条发会慢得离谱）。"""
        if pane is None:
            self.validate_pane(pane_id)
        if not keys:
            return
        self._run("send-keys", "-t", pane_id, *keys)

    def screen(self, pane_id: str, lines: int = 12) -> str:
        """Plain text of the pane's last lines (cheap, no history)."""
        try:
            return self._run("capture-pane", "-p", "-t", pane_id, "-S", f"-{max(1, lines)}")
        except TmuxError:
            return ""

    @staticmethod
    def submit_fingerprint(text: str) -> str:
        """The bit of text we look for in the input box after pressing Enter."""
        joined = " ".join((text or "").split())
        return joined[-40:] if joined else ""

    @staticmethod
    def _prompt_region_lines(screen: str, lines: int = 3) -> list[str]:
        """Lines of the prompt box: from the last ❯ line to the bottom, plus the bottom lines.

        The input box starts at the ❯ marker and can span many lines for a
        long message, so a fixed line count misses a fingerprint that
        straddles the last two input lines (the divider and status line sit
        below them).  The transcript lives above the ❯ line, so anchoring
        there keeps a submitted message out of the check; the bottom-lines
        fallback covers panes whose prompt does not use ❯ (python >>>,
        shells).  Both the wait and the post-press check use this region, so
        they can no longer disagree about what counts as the prompt.
        """
        screen_lines = screen.splitlines()
        if not screen_lines:
            return []
        start = len(screen_lines) - max(2, lines)
        prompt_at = None
        for index, line in enumerate(screen_lines):
            if "❯" in line:
                prompt_at = index
        if prompt_at is not None:
            start = min(start, prompt_at)
        return screen_lines[max(0, start):]

    @staticmethod
    def _fingerprint_matches(fingerprint: str, region: str) -> bool:
        """Match ignoring all whitespace: the screen wraps our text, so a line
        break can land mid-word and the joined region gains a space the text
        never had.  Comparing with whitespace stripped makes the wrap invisible.
        """
        needle = "".join(fingerprint.split())
        if not needle:
            return False
        return needle in "".join(region.split())

    def _screen_bottom(self, pane_id: str, lines: int = 3) -> str:
        """The prompt box area of the screen, whitespace-normalised for matching."""
        screen = self._run("capture-pane", "-p", "-t", pane_id) if hasattr(self, "_run") else ""
        return " ".join(" ".join(self._prompt_region_lines(screen, lines)).split())

    def still_in_prompt(self, pane_id: str, text: str, lines: int = 3) -> bool:
        """Is the text we just typed still sitting in the prompt box?

        Comparing whole screens is unreliable (TUIs repaint constantly, so "the
        screen changed" does not mean "it was submitted"); looking for our own
        text in the prompt box is the direct question.

        The prompt box starts at the ❯ marker and can span many lines for a
        long message, so the check takes everything from the last ❯ line to
        the bottom of the screen, plus the bottom lines as a fallback for
        prompts that do not use ❯.  A fixed 3-line window missed a fingerprint
        straddling the last two input lines (the divider and status line sit
        below them) and called a stuck multi-line paste submitted.  The
        transcript lives above the ❯ line, so anchoring there keeps a
        submitted message out of the check: after a submit the message stays
        visible in the transcript just above the input box, and a wider
        unanchored window would report a false not-submitted.
        """
        return self.fingerprint_in_prompt(pane_id, self.submit_fingerprint(text), lines)

    def fingerprint_in_prompt(self, pane_id: str, fingerprint: str, lines: int = 3) -> bool:
        """Is the stored fingerprint still sitting in the prompt box?

        Same check as still_in_prompt, but for a fingerprint instead of the
        full text: the background retry loop keeps only the fingerprint (it
        never stores the message), so this is the question it asks every
        round before pressing Enter again.
        """
        if not fingerprint:
            return False
        return self._fingerprint_matches(fingerprint, self._screen_bottom(pane_id, lines))

    def wait_for_paste_visible(self, pane_id: str, text: str,
                               polls: int = 20, interval: float = 0.1) -> bool:
        """Wait until our pasted text shows up in the prompt box.

        A paste bigger than the TUI can digest is still landing when the first
        C-m fires, and the key is dropped.  The length-based delay in
        press_enter_until_submitted is only a guess; this watches the screen
        instead.  Returns False when the text never shows (a pane whose prompt
        hides it, say) - the caller then presses anyway, so a missed
        fingerprint can never block a submit.
        """
        fingerprint = self.submit_fingerprint(text)
        if not fingerprint:
            return False
        for index in range(max(0, polls)):
            try:
                if self._fingerprint_matches(fingerprint, self._screen_bottom(pane_id)):
                    return True
            except TmuxError:
                pass
            if index + 1 < polls:
                time.sleep(interval)
        return False

    def press_enter_until_submitted(self, pane_id: str, text: str, python: bool = False,
                                    tries: int = 3) -> int:
        """Press Enter until the text actually leaves the prompt box.

        A paste bigger than the TUI can digest makes the first C-m land while it
        is still consuming input, and the key is dropped - the "text is in the
        box but nothing was sent" report.  Waiting longer is a guess; watching our
        own text in the bottom rows is not.  An extra Enter on an empty prompt is
        a no-op, so a false positive costs nothing.
        """
        delay = 0.12 + (0.25 if python else 0.0) + min(1.8, len(text) / 2500.0)
        time.sleep(delay)
        # Do not press into a paste that has not landed yet: one capture that
        # already shows the text is worth more than the guessed delay above.
        # A miss just falls through to the old press-and-check loop.
        self.wait_for_paste_visible(pane_id, text)
        presses = 0
        for attempt in range(max(1, tries)):
            self._run("send-keys", "-t", pane_id, "C-m")
            presses += 1
            if attempt + 1 >= tries:
                break
            time.sleep(0.5)
            try:
                if not self.still_in_prompt(pane_id, text):
                    break
            except TmuxError:
                break
        return presses

    def send(self, pane_id: str, text: str, submit: bool = True, pane: dict | None = None) -> bool:
        """Paste text into a pane and, when asked, press Enter until it submits.

        Returns True when the text is in (and, for submit, no longer sitting in
        the prompt box).  Returns False instead of raising when the Enter
        presses never move it: the paste itself succeeded, so the caller must
        report "pasted but not submitted", not a failed send.
        """
        if pane is None:
            pane = self.validate_pane(pane_id)
        buffer_name = f"tmux-web-{secrets.token_hex(8)}"
        submitted = True
        presses = 0
        try:
            if submit:
                self._run("send-keys", "-t", pane_id, "C-u")
                time.sleep(0.05)
            self._run("load-buffer", "-b", buffer_name, "-", input_bytes=text.encode())
            self._run("paste-buffer", "-b", buffer_name, "-d", "-t", pane_id)
            if submit:
                presses = self.press_enter_until_submitted(
                    pane_id, text, python=pane.get("command") == "python3", tries=5)
                try:
                    submitted = not self.still_in_prompt(pane_id, text)
                except TmuxError:
                    submitted = False
                if not submitted:
                    LOGGER.warning("paste not submitted after %d enters: pane=%s", presses, pane_id)
        finally:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                capture_output=True,
                check=False,
            )
        return submitted


class BackgroundResend:
    """Retry Enter in the background until a stuck paste submits.

    The in-request retry (#11) gives up after 5 tries; this picks up where
    it left off.  One task per pane, at most: a new send to the same pane
    supersedes the old task, so its Enter can never land in the new
    message's paste.  Only the fingerprint is kept, never the text.

    The loop only presses while the fingerprint is visibly in the prompt
    box, so it cannot hammer Enter into a permission dialog: the moment
    the text leaves the prompt it stops.  It stops, too, after time_limit
    (5 minutes) or when the pane is gone.  A restart drops the in-memory
    state - the client then shows "pasted but not submitted" instead of
    pretending the retry is still running.

    Records are keyed by the pane ref the routes already use (the chat and
    share routes pass the pane id; the relay rebuilds it from the resolved
    target), so a later send to the same pane finds and supersedes them.
    All dict access happens on the event loop; the blocking tmux/SSH calls
    go through asyncio.to_thread.
    """

    def __init__(self, interval: float = 2.0, time_limit: float = 300.0):
        self.interval = interval
        self.time_limit = time_limit
        self._resolver = None
        self._records: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def bind(self, resolver):
        """resolver(pane_id) -> (transport, actual_pane_id), as pane_transport."""
        self._resolver = resolver

    @staticmethod
    def _key(pane_id: str) -> str:
        return str(pane_id or "").strip()

    def status(self, pane_id: str) -> dict | None:
        """The public record for a pane: status, presses and reason only.

        The fingerprint is deliberately left out - the transcript response
        carries this record, and the text fragment must never leak there.
        """
        record = self._records.get(self._key(pane_id))
        if record is None:
            return None
        return {"status": record["status"], "presses": record["presses"],
                "reason": record.get("reason")}

    def start(self, pane_id: str, fingerprint: str) -> dict | None:
        """Start (or restart) the background retry for a pane."""
        if not fingerprint:
            return None
        key = self._key(pane_id)
        self.cancel(key, "superseded")
        record = {"pane": key, "status": "retrying", "presses": 0,
                  "reason": None, "fingerprint": fingerprint,
                  "generation": self._records.get(key, {}).get("generation", 0) + 1,
                  "started_at": time.monotonic()}
        self._records[key] = record
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop to run on (a sync caller): fail at once, never pretend.
            record["status"] = "failed"
            record["reason"] = "no event loop"
            return self.status(key)
        self._tasks[key] = loop.create_task(self._run(key, record))
        return self.status(key)

    def cancel(self, pane_id: str, reason: str = "superseded") -> dict | None:
        """Mark a pane's retry superseded and stop its task.  A no-op when idle."""
        key = self._key(pane_id)
        record = self._records.get(key)
        if record is None:
            return None
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        if record["status"] == "retrying":
            record["status"] = "superseded"
            record["reason"] = reason
        return self.status(key)

    async def _run(self, key: str, record: dict) -> None:
        fingerprint = record["fingerprint"]
        started_at = record["started_at"]
        try:
            if self._resolver is None:
                record["status"] = "failed"
                record["reason"] = "not bound"
                LOGGER.warning("background resend stopped, manager is not bound: pane=%s", key)
                return
            while True:
                if time.monotonic() - started_at > self.time_limit:
                    record["status"] = "failed"
                    record["reason"] = "timeout"
                    LOGGER.warning("background resend gave up after %d enters (%.0fs): pane=%s",
                                   record["presses"], self.time_limit, key)
                    return
                if self._records.get(key) is not record or record["status"] != "retrying":
                    return
                try:
                    transport, actual = self._resolver(key)
                    check = getattr(transport, "fingerprint_in_prompt", None)
                    if check is None:
                        record["status"] = "failed"
                        record["reason"] = "unsupported transport"
                        LOGGER.warning("background resend stopped, transport has no fingerprint check: pane=%s", key)
                        return
                    in_prompt = await asyncio.to_thread(check, actual, fingerprint)
                except asyncio.CancelledError:
                    raise
                except TmuxError:
                    record["status"] = "failed"
                    record["reason"] = "pane gone"
                    LOGGER.warning("background resend stopped, pane gone: pane=%s", key)
                    return
                except HTTPException:
                    # The resolver could not find the host or pane any more.
                    record["status"] = "failed"
                    record["reason"] = "pane gone"
                    LOGGER.warning("background resend stopped, pane gone: pane=%s", key)
                    return
                except (RuntimeError, OSError) as exc:
                    # A flaky capture or ssh hiccup: skip this round, keep going.
                    LOGGER.debug("background resend check failed, will retry: pane=%s error=%s", key, exc)
                    await asyncio.sleep(self.interval)
                    continue
                # The task may have been superseded while the check was running:
                # re-check before pressing, so a stale Enter can never land in
                # the new message's paste.
                if self._records.get(key) is not record or record["status"] != "retrying":
                    return
                if not in_prompt:
                    record["status"] = "submitted"
                    record["reason"] = None
                    LOGGER.info("background resend submitted after %d enters: pane=%s",
                                record["presses"], key)
                    return
                try:
                    await asyncio.to_thread(transport.send_key, actual, "Enter")
                except asyncio.CancelledError:
                    raise
                except TmuxError:
                    record["status"] = "failed"
                    record["reason"] = "pane gone"
                    LOGGER.warning("background resend stopped, pane gone: pane=%s", key)
                    return
                except (RuntimeError, OSError) as exc:
                    LOGGER.debug("background resend press failed, will retry: pane=%s error=%s", key, exc)
                    await asyncio.sleep(self.interval)
                    continue
                record["presses"] += 1
                await asyncio.sleep(self.interval)
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)


def retry_or_fail(manager: BackgroundResend | None, pane_id: str, text: str) -> tuple[str, bool]:
    """Map a failed in-request submit to the background retry.

    Returns (status, retrying): "retrying" when a background task started,
    "pasted_not_submitted" when there is nothing to retry with (an empty
    fingerprint, or no manager at all).  The three message routes share
    this so the failure wording stays identical.
    """
    fingerprint = TmuxBackend.submit_fingerprint(text)
    if manager is None or not fingerprint:
        return "pasted_not_submitted", False
    manager.start(pane_id, fingerprint)
    return "retrying", True


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


def guard_panel_path(path: str) -> str:
    """File-panel deletes and renames must not aim at a root, home, or a top-level dir."""
    cleaned = os.path.normpath(os.path.expanduser(str(path or "").strip()))
    if cleaned in {"", os.sep} or cleaned == os.path.expanduser("~"):
        raise ValueError("拒绝操作文件系统根目录或整个家目录")
    if cleaned.count(os.sep) < 2:
        raise ValueError("路径范围太大，拒绝操作")
    return cleaned


class KeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=16)


class PromptChoiceRequest(BaseModel):
    """选一个菜单项（index）或者直接发一个键（key: esc/enter/yes/no/数字）。"""

    index: int | None = None
    key: str = ""
    toggle: int | None = None      # 第几个开关（[ Low ][ High ][ Max ] 那种）
    value: int | None = None       # 要切到第几个值


class TransferTicketRequest(BaseModel):
    """请求一张直连传输票据（走直连源站时替代 cookie 鉴权）。"""

    pane: str
    path: str
    direction: str = "down"        # down | up
    size: int = 0                  # 上传时给总字节数，票据据此限制
    name: str = ""


class FileRenameRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)


class FileDeleteRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)
    recursive: bool = False


class ExecRequest(BaseModel):
    """一口气跑完一条命令并拿到输出 —— 给远程调试的 agent 用（比抢一个交互 shell 快得多）。"""
    command: str = Field(min_length=1, max_length=20000)
    host: str = ""                       # 空 / local 表示本机，否则是 remote_hosts 里的 id 或名字
    cwd: str = ""
    timeout: int = Field(default=60, ge=1, le=600)
    max_output: int = Field(default=200_000, ge=1000, le=2_000_000)


class FedTokenRequest(BaseModel):
    """子节点签发的 auth key。ttl=0 表示永久（只能靠吊销收回）。"""
    label: str = Field(default="", max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["view"])
    panes: object = "all"                 # "all" | ["%12"] | {"favorite": true}
    ttl: int = Field(default=86400, ge=0, le=60 * 60 * 24 * 30)


class FedNodeRequest(BaseModel):
    """父节点记住的一个子节点：名字 + auth key（key 可带 @地址）+ 可选备用地址。"""
    label: str = Field(default="", max_length=64)
    prefix: str = Field(default="", max_length=16)      # 留空就从名字派生
    base_url: str = Field(default="", max_length=400)
    fallback_url: str = Field(default="", max_length=400)
    token: str = Field(default="", max_length=400)
    prefer: str = Field(default="direct", pattern="^(direct|public)$")   # 先走哪条线路


class FedFileRequest(BaseModel):
    """文件操作：路径 + 可选的新名字 / 递归标记。"""
    path: str = Field(min_length=1, max_length=4096)
    new_name: str = Field(default="", max_length=255)
    recursive: bool = False


class FedPreferRequest(BaseModel):
    """先走哪条线路。"""
    prefer: str = Field(default="direct", pattern="^(direct|public)$")


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
    # Optional: start the session already running a coding agent.
    agent: str = Field(default="", max_length=32)
    agent_args: str = Field(default="", max_length=512)
    create_dir: bool = True
    # Keep the pane alive (and say why) when the agent exits immediately.
    keep_open: bool = True


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
    # how commands reach the host: "ssh" (default) or "agent" (the WebSocket
    # agent program, installed over SSH); anything else is rejected below
    transport: str = Field(default="ssh", max_length=16)
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
    transport: str | None = None

    @field_validator("host_key", "fingerprint", mode="before")
    @classmethod
    def _empty_means_omitted(cls, value):
        # A host saved before its first successful connection stores an empty
        # key; the switch and edit-save payloads send that empty string back.
        # An empty string means "not provided" — never "clear the pinned key":
        # update_host keeps the stored value when it receives None.
        return None if value == "" else value


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


class HomePinRequest(BaseModel):
    pane: str = ""
    label: str = ""
    pinned: bool = True


class HomePinRenameRequest(BaseModel):
    label: str = ""


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


class ModelSwitchRequest(BaseModel):
    # empty is allowed for CLIs whose /model opens its own picker
    model: str = Field(default="", max_length=80)
    # which provider the model belongs to (Hermes keeps several in one config)
    provider: str = Field(default="", max_length=60)


class RelayMessageRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=100_000)
    submit: bool = True


_PS_CACHE: dict[str, object] = {"at": 0.0, "table": {}}


def local_process_table(ttl: float = 10.0) -> dict:
    """本机进程表（pid -> (ppid, argv)），10 秒内复用。

    列 pane 时一次 ps 就够，不用每个 pane 各查一遍。
    """
    now = time.monotonic()
    if now - float(_PS_CACHE["at"] or 0.0) < ttl:
        return dict(_PS_CACHE["table"] or {})
    try:
        raw = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        raw = ""
    table = transcript_reader.parse_process_table(raw)
    _PS_CACHE["at"] = now
    _PS_CACHE["table"] = table
    return dict(table)


def pane_metadata(pane: dict) -> dict:
    """Annotate a pane with its provider and what TmuxWeb can do with it.

    Clients can then hide actions that would only fail, instead of discovering
    support by triggering a 409.
    """
    provider = (pane.get("provider")
                or transcript_reader.provider_for_pane(pane, local_process_table()
                                                       if pane.get("host_id") in (None, "localhost") else None)
                or pane.get("provider") or transcript_reader.provider_for_command(pane.get("command")))
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
        self.process = None
        self._closed = False
        try:
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
            slave_fd = None
            self._set_size(cols, rows)
        except Exception:
            # A failed attach must not keep the pty: under fd pressure the
            # constructor itself is what raises (EMFILE from Popen), and the
            # two fresh descriptors would otherwise leak one pair per retry.
            self._close_failed_openpty(slave_fd)
            raise

    def _close_failed_openpty(self, slave_fd):
        """Best-effort cleanup after __init__ failed part-way through."""
        process = getattr(self, "process", None)
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self._closed = True

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


PLUGIN_MANAGER: dict = {}     # filled near the end of create_app: the MCP endpoint needs it earlier


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
    fed_store=None,
    fed_tokens=None,
    password_db: str | None = None,
    plugins_dir: str | None = None,
    plugins: str | None = None,
    plugin_state: str | None = None,
    resend_manager: BackgroundResend | None = None,
) -> FastAPI:
    plugin_manager = PLUGIN_MANAGER
    plugin_manager.clear()
    tmux = backend or TmuxBackend()
    # One background retry loop per pane, shared by the chat, share and relay
    # routes (and visible through the transcript polling channel).  Tests pass
    # their own manager to shrink the interval and the cap.
    resend_manager = resend_manager or BackgroundResend()
    configured_password = password if password is not None else os.environ.get("TMUX_WEB_PASSWORD")
    if not configured_password:
        raise RuntimeError("TMUX_WEB_PASSWORD must be set")
    secret = session_secret or os.environ.get("TMUX_WEB_SESSION_SECRET") or secrets.token_hex(32)
    # 直连传输票据：默认由会话密钥派生，所以不配也能用；独立配置便于轮换
    transfer_secret = (os.environ.get("TMUX_WEB_TRANSFER_SECRET")
                       or hashlib.sha256(("transfer:" + secret).encode()).hexdigest()).encode()
    transfer_ledger = transfer_tickets.Ledger()
    # The password can be changed from the console's settings panel; the store keeps a hash and,
    # once it exists, session tokens derive from that hash instead of the environment value.
    # A caller that passes `password` explicitly (the tests do) means "use exactly this": such an app must
    # not pick up the deployment's passwords.json, or a password changed from the UI would break every test.
    password_store = PasswordStore(
        password_db
        if password_db is not None
        else os.environ.get("TMUX_WEB_PASSWORD_DB")
        or (None if password is not None else str(BASE_DIR / "passwords.json"))
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
    # 远端下载现在是流式的，内存不再随文件大小增长：默认放到 512MB，视频之类才下得动
    download_limit = int(os.environ.get("TMUX_WEB_FILE_DOWNLOAD_MAX", str(512 * 1024 * 1024)))
    zip_limit = int(os.environ.get("TMUX_WEB_ZIP_MAX", str(file_browser.DEFAULT_ZIP_BYTES)))
    zip_max_files = int(os.environ.get("TMUX_WEB_ZIP_MAX_FILES", str(file_browser.DEFAULT_ZIP_FILES)))
    upload_limit = int(os.environ.get("TMUX_WEB_FILE_UPLOAD_MAX", str(256 * 1024 * 1024)))
    # 单次（total=0）上传会把整包读进内存，超过这个尺寸就要求走分块协议。
    # 4MB 太严：远端机器上还没升级的 relay 会直接 413，而 tailnet 上 16MB
    # 整包只要几秒。16MB 既保住了"慢链路 / Cloudflare 下必须分块"，也不误伤
    # 老客户端 —— 真正的 CF 保护来自分块本身，不是这个数。
    # 流式下载里「多久没有任何字节」算失败：90s 太长，HTTP 响应会先干等；
    # 25s 足够覆盖慢链路抖动，又能让浏览器早点拿到 503 而不是挂住。
    download_idle = float(os.environ.get("TMUX_WEB_DOWNLOAD_IDLE_SECONDS", "25"))
    single_shot_upload_max = int(
        os.environ.get("TMUX_WEB_SINGLE_UPLOAD_MAX", str(16 * 1024 * 1024)))
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
    # 联邦：fed_nodes = 我们拉谁的 pane；fed_tokens = 我们发出去的令牌
    fed_db = os.environ.get("TMUX_WEB_FED_DB", str(BASE_DIR / "federation.db"))
    # 测试进程里默认用内存库：federation.db 装的是用户真实接入的子节点，一次测试跑不该
    # 读到它（会让搜索/中继结果多出几台机器），更不该写它。要测真实的库就显式传 fed_store。
    if "unittest" in sys.modules and fed_db != ":memory:":
        fed_db = ":memory:"
    fed_nodes = fed_store or federation.FederationStore(fed_db)
    fed_grants = fed_tokens or federation.FedGrants(fed_db)
    # Set after the plugins load (below): the agent plugin may offer a
    # runner factory and a per-host status provider. A closure, so routes
    # defined above this line still read the reassigned value at call time.
    agent_status_provider = None

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

    @app.middleware("http")
    async def transfer_cors(request: Request, call_next):
        """票据路由 (/t/*) 的 CORS。

        页面在 Cloudflare 域名上，字节发到直连源站，属于跨源；而直连源站拿不到
        控制台的 cookie（不同 host），鉴权全靠票据，所以这里不需要 credentials，
        只要放行方法/头，并让前端能读到 X-Upload-Offset 这类响应头。
        """
        if not request.url.path.startswith("/t/"):
            return await call_next(request)
        origin = request.headers.get("origin", "")
        headers = {
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Methods": "GET, PUT, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Requested-With",
            "Access-Control-Expose-Headers": "X-Upload-Offset, Content-Length, Content-Disposition",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=headers)
        response = await call_next(request)
        for key, value in headers.items():
            response.headers[key] = value
        return response

    @app.middleware("http")
    async def versioned_asset_cache(request: Request, call_next):
        """A ?v= asset URL is immutable for a year; anything else keeps its own headers.

        The console is used over Cloudflare from places where one round trip costs
        a third of a second or more, so a repeat visit must not re-download every
        script. Only versioned URLs get the long cache: the version changes when
        the bytes do (see ASSET_VERSION in web/index.html and the console bundle
        hash), so a stale immutable copy can never be served under a fresh URL.
        """
        response = await call_next(request)
        if (request.url.path.startswith(("/assets/", "/share-assets/"))
                and response.status_code == 200
                and "v" in request.query_params):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

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

    PANE_NUMBER_RE = re.compile(r"^#?(\d{1,5})$")

    def pane_ref_for(number: int) -> str:
        """编号 → 带主机作用域的 pane 引用；没分出去的号就是 404。"""
        pane_id = home.pane_for_number(number)
        if not pane_id:
            raise HTTPException(status_code=404,
                                detail=f"没有 #{number} 这个窗口（编号按首次出现分配，不会回收）")
        return pane_id

    FED_REF_RE = re.compile(r"^([a-z][a-z0-9]{1,15})\s*[#:/]\s*(\d{1,5})$")

    def resolve_fed_ref(text: str) -> str | None:
        """`ddr#12` → `<node_id>~%12`；对端编号由对端自己维护。"""
        match = FED_REF_RE.match((text or "").strip().lower())
        if not match:
            return None
        node = fed_nodes.by_prefix(match.group(1))
        if node is None:
            raise HTTPException(status_code=404, detail=f"没有叫 {match.group(1)} 的联邦节点")
        wanted = int(match.group(2))
        # 只查已有的 hosts 快照：这里是同步函数，在事件循环里发 HTTP 去问自己会死锁
        # （对端如果是本进程，它要等同一个循环来回应）。快照由侧栏每 20 秒刷新。
        for host in (hosts_snapshot["value"] or {}).get("hosts") or []:
            if host.get("kind") != "fed" or host.get("prefix") != match.group(1):
                continue
            if host.get("status") != "online":
                raise HTTPException(status_code=503,
                                    detail=f"{match.group(1)} 现在连不上：{host.get('error') or 'offline'}")
            for pane in host.get("panes") or []:
                if int(pane.get("number") or 0) == wanted:
                    return pane["id"]
            raise HTTPException(status_code=404, detail=f"{match.group(1)}#{wanted} 不存在")
        raise HTTPException(status_code=404,
                            detail=f"还没有 {match.group(1)} 的 pane 列表（打开控制台让它刷新一次）")

    def resolve_pane_ref(pane_ref: str) -> str:
        """`#12` / `12` / `ddr#12` 解析成真正的 pane 引用，其他写法原样返回。"""
        text = str(pane_ref or "").strip()
        federated = resolve_fed_ref(text)
        if federated:
            return federated
        match = PANE_NUMBER_RE.match(text)
        return pane_ref_for(int(match.group(1))) if match else text

    def fed_node(host_id: str) -> dict | None:
        """host_id 是联邦节点就返回它（含令牌），否则 None。"""
        if not host_id or host_id == "localhost":
            return None
        try:
            return fed_nodes.get_node(host_id, include_token=True)
        except KeyError:
            return None

    def fed_prefix_of(host_id: str) -> str:
        """`<node_id>` → `ddr`；不是联邦节点就是空串。"""
        node = fed_node(host_id)
        return str((node or {}).get("prefix") or "")

    def fed_transport(host_id: str):
        node = fed_node(host_id)
        if node is None:
            return None
        return federation.FedTransport(node)

    def pane_or_404(pane_id: str) -> dict:
        pane_id = resolve_pane_ref(pane_id)
        try:
            host_id, actual_pane_id = split_pane_ref(pane_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="invalid pane reference") from exc
        if host_id != "localhost":
            federated = fed_transport(host_id)
            if federated is not None:
                try:
                    return federated.validate_pane(actual_pane_id)
                except (federation.FederationError, RuntimeError) as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
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

    def pane_provider(pane: dict) -> str:
        """Same resolution the pane listing uses (process tree first, then command)."""
        return (pane.get("provider")
                or transcript_reader.provider_for_pane(
                    pane, local_process_table() if pane.get("host_id") in (None, "localhost") else None)
                or transcript_reader.provider_for_command(pane.get("command")) or "")

    def pane_transport(pane_ref: str):
        pane_ref = resolve_pane_ref(pane_ref)
        try:
            host_id, pane_id = split_pane_ref(pane_ref)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="invalid pane reference") from exc
        if host_id == "localhost":
            return tmux, pane_id
        federated = fed_transport(host_id)
        if federated is not None:
            return federated, pane_id
        try:
            host = remote_hosts.get_host(host_id, include_credential=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="remote host not found") from exc
        return runner_for(host), pane_id

    # The background retry loop resolves its pane fresh every round through
    # this, so it never holds a stale runner across a host change.
    resend_manager.bind(pane_transport)

    def relay_target(target: str):
        """Resolve a relay target to (transport, pane_id, pane, host_name)."""
        target = resolve_pane_ref(target)
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

    def relay_hosts(include_fed: bool = True) -> list[tuple[str, str, object, list[dict]]]:
        """Every reachable transport plus its panes, for relay/status discovery.

        ``include_fed=False`` 用在"对外共享"的地方：我们只再分享自己拥有的主机，
        不把借来的联邦节点转手出去 —— 而且自指节点（把自己当节点加进来）会因此
        变成无限递归。
        """
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
        for node in (fed_nodes.listing() if include_fed else []):
            if not node.get("enabled", True):
                continue
            try:
                full = fed_nodes.get_node(node["id"], include_token=True)
                federated = federation.FedTransport(full)
                found.append((node["id"], node.get("label") or node["prefix"], federated,
                              federated.list_panes()))
            except (KeyError, federation.FederationError, RuntimeError, OSError):
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

        # One round trip per host: the tail text is classified locally
        # afterwards, and the session-file verdict comes back in the same
        # packet. Agent transports send capture + status as one batch;
        # anything without capture_and_status_many runs the same two
        # calls as before, one each.
        captured: dict[int, dict] = {}
        native: dict[str, str] = {}
        def grab_both(group):
            key, transport, host_id, pane_ids, panes, status_ids = group
            try:
                both = getattr(transport, "capture_and_status_many", None)
                if both is not None:
                    texts, states = both(pane_ids, 40, status_ids=status_ids, panes=panes)
                else:
                    texts = transport.capture_many(pane_ids, 40, panes=panes)
                    states = (transport.status_many(status_ids)
                              if status_ids and hasattr(transport, "status_many") else {})
            except Exception:  # noqa: BLE001 - fall back to per-pane captures
                return key, {}, []
            # host-scoped: "%8" exists on several hosts and means a different pane
            return key, texts, [
                (f"{host_id}~{pane_id}" if "~" not in str(pane_id) else str(pane_id), state)
                for pane_id, state in states.items()]
        groups: dict[int, tuple] = {}
        for job in jobs:
            host_id, _, transport, pane = job
            actual = pane.get("pane_id") or pane["id"]
            entry = groups.setdefault(
                id(transport), (id(transport), transport, host_id, [], {}, []))
            entry[3].append(actual)
            entry[4][actual] = pane
            provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
            if host_id != "localhost" and provider and hasattr(transport, "status_many"):
                entry[5].append(actual)
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(groups) or 1))) as pool:
            for index, texts, listing in pool.map(grab_both, list(groups.values())):
                captured[index] = texts
                for key, state in listing:
                    native[key] = state

        # Ask each agent's own session file whether its turn is running: the screen lies
        # (a status bar saying "thinking: high" is a model setting, a TUI redraws while
        # idle) while the session file records the turn boundary. Remote verdicts
        # already came back with the captures above; only localhost is probed here.
        local_agents = [job[3] for job in jobs
                        if job[0] == "localhost"
                        and (job[3].get("provider")
                             or transcript_reader.provider_for_command(job[3].get("command")))]
        if local_agents:
            def probe_local():
                out = []
                for pane in local_agents:
                    provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
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

        for key, state in probe_local():
            native[key] = state

        busy_window = float(os.environ.get("TMUX_WEB_ACTIVITY_BUSY_SECONDS", "3"))
        now = time.time()

        def describe(job):
            host_id, host_name, transport, pane = job
            provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
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
                    state = relay.inspect(transport, actual_pane_id, provider, lines=40, pane=pane)
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
            states = list(pool.map(describe, jobs))
        # 编号在这里统一带上：relay / 主页 / MCP 拿到的 peer 都自带 #N
        try:
            numbers = home.ensure_numbers([state["id"] for state in states if state.get("id")])
        except (sqlite3.Error, ValueError):
            numbers = {}
        for state in states:
            number = numbers.get(state.get("id"))
            if number:
                state["number"] = number
                state["handle"] = f"#{number}"
        return states

    # ``collect_pane_states`` inspects every pane on every host; the console polls
    # it every 9s and the Home board needs it for its counters. Cache it briefly and
    # refresh in the background so a page load never pays for the whole sweep.
    pane_state_cache: dict = {"at": 0.0, "states": []}
    pane_state_lock = threading.Lock()
    pane_state_busy = {"flag": False}
    # The last sweep that finished, for the adaptive interval below.
    pane_state_last: dict = {"finished_at": 0.0, "duration": 0.0}
    # The in-flight sweep's completion event: waiters join it instead of each
    # waiting on a private event that nobody ever sets.
    pane_state_event: dict = {"event": None}

    def _refresh_pane_states() -> None:
        started_at = time.time()
        try:
            states = collect_pane_states()
            with pane_state_lock:
                pane_state_cache.update({"at": time.time(), "states": states})
            elapsed = time.time() - started_at
            pane_state_last.update({"finished_at": time.time(), "duration": elapsed})
            log = LOGGER.warning if elapsed > 3 else LOGGER.info
            log("pane state sweep: %d panes in %.1fs", len(states), elapsed)
        except Exception:  # noqa: BLE001 - a failed sweep must never kill the poller
            LOGGER.warning("pane state sweep failed after %.1fs", time.time() - started_at, exc_info=True)
        finally:
            pane_state_busy["flag"] = False

    def pane_states_cached(wait: float = 3.0, ttl: float = 30.0, stale_ok: bool = False) -> list[dict]:
        """Last known pane states, refreshed once they are older than ``ttl``.

        ``wait`` bounds how long a caller blocks for a fresh sweep; a warm cache
        returns at once and a cold one returns whatever is already there after
        the deadline, while the sweep keeps running in the background.

        With ``stale_ok`` a stale but non-empty cache answers at once (the sweep
        refreshes behind it); only a cold cache waits, up to ``wait``. That is
        what /api/panes/status wants: the sweep takes seconds on a many-pane
        host, so waiting for it made every status poll cost the full deadline.

        A sweep also backs off after a slow one: no new sweep starts until at
        least ``TMUX_WEB_SWEEP_MIN_INTERVAL`` (default 60s) or twice the last
        sweep's duration (``TMUX_WEB_SWEEP_BACKOFF_FACTOR``, default 2) has
        passed since the last one finished. A 24s sweep used to re-run 30s
        later, so the tmux servers never idled and user ops queued behind
        sweep commands; freshness drops instead — performance first.
        """
        with pane_state_lock:
            fresh = (time.time() - pane_state_cache["at"]) < ttl
            states = pane_state_cache["states"]
            finished_at = pane_state_last["finished_at"]
            last_duration = pane_state_last["duration"]
        if fresh:
            return states
        if finished_at:
            min_interval = max(
                float(os.environ.get("TMUX_WEB_SWEEP_MIN_INTERVAL", "60")),
                float(os.environ.get("TMUX_WEB_SWEEP_BACKOFF_FACTOR", "2"))
                * last_duration)
            if time.time() - finished_at < min_interval:
                return states

        def refresh(event: threading.Event) -> None:
            try:
                _refresh_pane_states()
            finally:
                event.set()

        with pane_state_lock:
            should_start = not pane_state_busy["flag"]
            if should_start:
                pane_state_busy["flag"] = True
                event = threading.Event()
                pane_state_event["event"] = event
            else:
                event = pane_state_event["event"]
        if should_start:
            threading.Thread(target=refresh, args=(event,), daemon=True).start()
        if stale_ok and states:
            # Answer from the cache at once; the sweep refreshes behind us.
            return states
        if event is not None:
            event.wait(max(0.0, wait))
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
            data = transport.transcript(actual_pane_id, 1, pane=pane)
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
        theme = home.get_prefs().get("theme", "")
        if theme:
            # the account's theme travels in a readable cookie so the next page paints in it immediately
            response.set_cookie(THEME_COOKIE, theme, samesite="lax", max_age=60 * 60 * 24 * 365, path="/")
        return response

    @app.get("/api/prefs")
    async def read_prefs(request: Request):
        require_auth(request)
        return {"prefs": home.get_prefs()}

    @app.put("/api/prefs")
    async def write_prefs(request: Request):
        """UI preferences that follow the account (theme today), so every device shows the same console."""
        require_auth(request)
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="expected an object of preferences")
        allowed = {"theme", "sidebar", "preview", "prompt_card"}
        # preview = 侧栏悬停预览的延迟（off 关闭；1/2/3 秒）
        # prompt_card = 聊天里的 agent 交互菜单卡片（实验性，默认 off）
        choices = {"sidebar": {"pane", "none"}, "preview": {"off", "1", "2", "3"},
                   "prompt_card": {"off", "on"}}
        saved = home.get_prefs()
        for key, value in payload.items():
            if key not in allowed:
                raise HTTPException(status_code=400, detail=f"unknown preference {key}")
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(status_code=400, detail=f"preference {key} must be a non-empty string")
            if key in choices and value not in choices[key]:
                raise HTTPException(status_code=400,
                                    detail=f"preference {key} must be one of {sorted(choices[key])}")
            stored = home.set_pref(key, value)
            saved = stored
        response = JSONResponse({"prefs": saved})
        if "theme" in saved:
            response.set_cookie(THEME_COOKIE, saved["theme"], samesite="lax", max_age=60 * 60 * 24 * 365, path="/")
        return response

    _tailnet_name: dict = {"at": 0.0, "name": ""}

    def tailnet_name() -> str:
        """MagicDNS name for this node (empty when tailscale is absent)."""
        import subprocess as sp
        import time as _time

        cached = _tailnet_name
        if _time.time() - cached["at"] < 600:
            return cached["name"]
        name = ""
        try:
            raw = sp.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                         timeout=5).stdout
            name = (json.loads(raw).get("Self") or {}).get("DNSName") or ""
        except (OSError, sp.SubprocessError, ValueError):
            name = ""
        _tailnet_name.update({"at": _time.time(), "name": name.rstrip(".")})
        return cached["name"] if False else _tailnet_name["name"]

    def tailscale_serve_active() -> bool:
        import subprocess as sp
        try:
            out = sp.run(["tailscale", "serve", "status"], capture_output=True, text=True,
                         timeout=5).stdout
        except (OSError, sp.SubprocessError):
            return False
        return bool(out.strip()) and "No serve config" not in out

    def local_addresses() -> list[dict]:
        """Ways to reach this console: LAN, tailnet, and the public URL if set.

        A user on the same LAN or tailnet can skip Cloudflare entirely - no
        100 MB body cap, no 100 s request limit, and the file never leaves the
        private network.  We only *list* the entries; picking one is the user's
        decision (see the 直连 block in settings).
        """
        import subprocess as sp

        found: list[dict] = []

        def add(url: str, kind: str, label: str):
            if url and all(item["url"] != url for item in found):
                found.append({"url": url, "kind": kind, "label": label})

        port = os.environ.get("TMUX_WEB_PORT", "8787")
        try:
            listed = sp.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                            capture_output=True, text=True, timeout=5).stdout
        except (OSError, sp.SubprocessError):
            listed = ""
        # 只认真实网卡：docker0/br-*/veth/tun 这些地址用户永远连不上，列出来只会误导
        for line in listed.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2] != "inet":
                continue
            interface = parts[1].rstrip(":")
            if interface.startswith(("docker", "br-", "veth", "virbr", "tun", "tap", "tailscale", "lo")):
                continue
            address = parts[3].split("/")[0]
            if address.startswith(("169.254.", "172.17.")):
                continue
            add(f"http://{address}:{port}", "lan", f"局域网 · {address}")
        try:
            tail = sp.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, sp.SubprocessError):
            tail = ""
        if tail:
            add(f"http://{tail}:{port}", "tailnet", f"Tailscale · {tail}")
            # 只有真的起了 tailscale serve 才列 HTTPS 那条，否则点进去是连不上的
            name = os.environ.get("TMUX_WEB_TAILNET_NAME", "").strip()
            if not name and tailscale_serve_active():
                name = tailnet_name()
            if name:
                add(f"https://{name}", "tailnet", f"Tailscale HTTPS · {name}")
        public = (os.environ.get("TMUX_WEB_PUBLIC_URL") or "").strip()
        if public:
            add(public.rstrip("/"), "public", "公网（Cloudflare）")
        # 额外中转（aliyun 反代之类）用逗号隔开配进来，前端会自动去探测
        extra = (os.environ.get("TMUX_WEB_TRANSFER_ORIGINS") or "").strip()
        for item in extra.split(","):
            url = item.strip().rstrip("/")
            if url:
                add(url, "transfer", f"中转 · {urllib.parse.urlparse(url).netloc}")
        # 只有 https 的源站能当传输目标：https 页面对 http 的 fetch/下载会被浏览器拦掉
        for item in found:
            item["transfer"] = item["url"].startswith("https://") and item["kind"] != "public"
        return found

    @app.get("/api/system/addresses")
    async def system_addresses(request: Request):
        """Direct entries for this install; the UI shows them in 设置 › 直连."""
        require_auth(request)
        entries = await asyncio.to_thread(local_addresses)
        host = request.headers.get("host", "")
        current = next((item for item in entries
                        if urllib.parse.urlparse(item["url"]).netloc == host), None)
        return {"host": host, "current_kind": current["kind"] if current else "public",
                "addresses": entries}

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

    @app.post("/api/session")
    async def create_session_cookie(request: Request):
        # Token -> cookie exchange for browser clients. A WebSocket
        # handshake cannot carry an Authorization header, and the session
        # cookie is the only WS credential, so a client that logged in with
        # an API token mints the cookie here. Without a valid token (or
        # without a configured api_token) this is 401, like every other
        # API route; a cookie holder just refreshes their cookie.
        require_auth(request)
        response = JSONResponse({"ok": True})
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
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__CONSOLE_BUNDLE_HASH__", CONSOLE_BUNDLE_HASH),
                            headers={"Cache-Control": "no-store"})

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

    async def run_exec(host_ref: str, command: str, workdir: str, timeout: int,
                       max_output: int) -> dict:
        """跑一条命令并带回收敛后的结果；路由和 MCP 的 run_command 共用。"""
        started = time.monotonic()

        def clip(blob: bytes) -> tuple[str, bool]:
            cut = blob[:max_output]
            return cut.decode("utf-8", "replace"), len(blob) > len(cut)

        if host_ref in ("", "local", "localhost", "self"):
            directory = workdir or str(BASE_DIR)

            def local_run():
                return subprocess.run(["bash", "-lc", command], cwd=directory,
                                      capture_output=True, timeout=timeout)

            try:
                completed = await asyncio.to_thread(local_run)
            except subprocess.TimeoutExpired:
                return {"host": "local", "cwd": directory, "exit": 124, "stdout": "",
                        "stderr": f"timeout after {timeout}s", "truncated": False,
                        "duration": round(time.monotonic() - started, 3)}
            except (OSError, ValueError) as exc:
                raise McpToolError(str(exc)) from exc
            host_label = "local"
        else:
            host = next((item for item in remote_hosts.list_hosts()
                         if host_ref in (item.get("id"), item.get("name"))), None)
            if host is None:
                raise McpToolError(f"没有这个主机：{host_ref}")
            runner = runner_for(host)
            wired = (f"cd {shlex.quote(workdir)} && " if workdir else "") + command
            try:
                completed = await asyncio.to_thread(runner.run, wired, timeout=timeout + 5)
            except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
                return {"host": host.get("name") or host.get("id"), "cwd": workdir, "exit": 124,
                        "stdout": "", "stderr": str(exc), "truncated": False,
                        "duration": round(time.monotonic() - started, 3)}
            host_label = host.get("name") or host.get("id")

        stdout, cut_out = clip(completed.stdout or b"")
        stderr, cut_err = clip(completed.stderr or b"")
        return {"host": host_label, "cwd": workdir, "exit": completed.returncode,
                "stdout": stdout, "stderr": stderr, "truncated": cut_out or cut_err,
                "duration": round(time.monotonic() - started, 3)}

    @app.post("/api/exec")
    async def exec_remote_command(request: Request, payload: ExecRequest):
        """跑一条命令、立刻拿到 stdout/stderr/退出码（给远程调试的 agent 用）。"""
        require_auth(request)
        if (os.environ.get("TMUX_WEB_EXEC") or "on").strip().lower() in {"0", "off", "false", "no"}:
            raise HTTPException(status_code=403, detail="exec 已被 TMUX_WEB_EXEC=off 关闭")
        try:
            return await run_exec((payload.host or "").strip(), payload.command,
                                  (payload.cwd or "").strip(), payload.timeout, payload.max_output)
        except McpToolError as exc:
            status = 404 if "没有这个主机" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    # ---- 联邦（被集成端）：签发令牌、把共享的 pane 只读地给出去 -------------
    def require_fed(request: Request, scope: str) -> dict:
        header = (request.headers.get("authorization") or "").strip()
        token = header[7:].strip() if header.lower().startswith("bearer ") else \
            (request.headers.get("x-tmuxweb-token") or "").strip()
        try:
            grants = fed_grants.verify(token)
        except federation.FederationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if scope not in grants["scopes"]:
            raise HTTPException(status_code=403,
                                detail=f"这个联邦令牌没有 {scope} 权限（现有：{' '.join(grants['scopes'])}）")
        return grants

    @app.get("/api/fed/ping")
    async def fed_ping():
        """不鉴权：回答"我是 TmuxWeb"，顺手给出可以直连的地址（内网/tailnet）。

        只列地址，不含任何凭据：父节点拿到之后就优先走直连，绕开 Cloudflare。
        """
        try:
            entries = await asyncio.to_thread(local_addresses)
        except Exception:                                  # 拿不到地址不影响探测
            entries = []
        return {"tmuxweb": True, "host": socket.gethostname(), "version": "1",
                "addresses": [{"url": item["url"], "kind": item["kind"]} for item in entries]}

    @app.get("/api/fed/panes")
    async def fed_panes(request: Request):
        grants = require_fed(request, "view")
        found = []
        snapshot = hosts_snapshot["value"]
        if snapshot and (time.time() - hosts_snapshot["at"]) < hosts_snapshot["ttl"]:
            # 刚被 /api/hosts 探过：直接用，别让一个联邦读者把整台机的 SSH 都拉一遍
            groups = [(host.get("id"), host.get("name") or host.get("id"), host.get("panes") or [])
                      for host in snapshot.get("hosts") or [] if host.get("kind") != "fed"]
        else:
            groups = [(host_id, host_name, panes) for host_id, host_name, _transport, panes
                      in await asyncio.to_thread(relay_hosts, False)]
        for host_id, host_name, panes in groups:
            for pane in panes:
                pane_id = pane.get("pane_id") or pane.get("id") or ""
                if not pane_id or not federation.pane_allowed(grants, pane_id):
                    continue
                found.append({
                    "id": pane_id, "number": None, "host": host_name,
                    "session": pane.get("session"), "window": pane.get("window_name") or "",
                    "index": pane.get("index"), "title": pane.get("title"),
                    "command": pane.get("command"), "path": pane.get("path"),
                    "active": pane.get("active"), "provider": pane.get("provider"),
                })
        numbers = await asyncio.to_thread(home.ensure_numbers, [item["id"] for item in found])
        for item in found:
            item["number"] = numbers.get(item["id"])
        return {"panes": found, "node": socket.gethostname(),
                "scopes": grants["scopes"]}

    @app.get("/api/fed/panes/{pane_id}/capture")
    async def fed_capture(request: Request, pane_id: str, history: int = 80):
        grants = require_fed(request, "view")
        actual = resolve_pane_ref(pane_id)
        if not federation.pane_allowed(grants, actual):
            raise HTTPException(status_code=403, detail="这个 pane 没有共享出去")
        transport, actual_id = pane_transport(actual)
        try:
            content = await asyncio.to_thread(transport.capture, actual_id, max(20, min(history, 500)),
                                              visible_only=True)
        except (TmuxError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"pane": actual, "content": content}

    @app.get("/api/fed/panes/{pane_id}/transcript")
    async def fed_transcript(request: Request, pane_id: str, limit: int = 200):
        grants = require_fed(request, "view")
        actual = resolve_pane_ref(pane_id)
        if not federation.pane_allowed(grants, actual):
            raise HTTPException(status_code=403, detail="这个 pane 没有共享出去")
        transport, actual_id = pane_transport(actual)
        pane = await asyncio.to_thread(pane_or_404, actual)
        try:
            payload = await asyncio.to_thread(transport.transcript, actual_id,
                                              max(1, min(limit, 1000)), False, pane)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return payload

    # ---- 子节点的读写：发按键（input 作用域）和文件（files 作用域）-----------
    FED_FILE_LIMIT = 512 * 1024 * 1024

    def fed_abs_path(value: str) -> str:
        try:
            return str(file_browser.resolve_path(value, os.path.expanduser("~")))
        except file_browser.FileBrowserError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def fed_pane_or_403(grants: dict, pane_id: str) -> str:
        actual = resolve_pane_ref(pane_id)
        if not federation.pane_allowed(grants, actual):
            raise HTTPException(status_code=403, detail="这个窗格没有共享出去")
        return actual

    @app.post("/api/fed/panes/{pane_id}/input")
    async def fed_input(request: Request, pane_id: str, payload: InputRequest):
        """往共享出去的窗格里送一段文字（等价于父节点在聊天里发消息）。"""
        grants = require_fed(request, "input")
        actual = fed_pane_or_403(grants, pane_id)
        pane = await asyncio.to_thread(pane_or_404, actual)
        transport, actual_id = pane_transport(actual)
        try:
            submitted = await asyncio.to_thread(transport.send, actual_id, payload.text,
                                                payload.submit, pane)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "sent", "pane": actual, "submitted": submitted is not False}

    @app.post("/api/fed/panes/{pane_id}/key")
    async def fed_key(request: Request, pane_id: str, payload: KeyRequest):
        grants = require_fed(request, "input")
        if payload.key not in ALLOWED_KEYS:
            raise HTTPException(status_code=422, detail="unsupported key")
        actual = fed_pane_or_403(grants, pane_id)
        pane = await asyncio.to_thread(pane_or_404, actual)
        transport, actual_id = pane_transport(actual)
        try:
            await asyncio.to_thread(transport.send_key, actual_id, payload.key, pane=pane)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "sent", "pane": actual, "key": payload.key}

    @app.get("/api/fed/files")
    async def fed_files(request: Request, path: str = "", hidden: int = 0, limit: int = 2000):
        """列一个目录。子节点的文件是主机级的（和 SSH 主机一样），不跟窗格绑定。"""
        require_fed(request, "files")
        directory = fed_abs_path(path or "~")
        try:
            data = await asyncio.to_thread(file_browser.list_directory, directory,
                                           bool(hidden), max(1, min(limit, 5000)))
        except (OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return data

    @app.get("/api/fed/files/stat")
    async def fed_stat(request: Request, path: str):
        require_fed(request, "files")
        target = Path(fed_abs_path(path))
        try:
            info = target.stat()
        except OSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"path": str(target), "size": info.st_size, "mtime": info.st_mtime,
                "is_dir": target.is_dir()}

    @app.get("/api/fed/files/raw")
    @app.get("/api/fed/files/download")
    async def fed_send_file(request: Request, path: str, inline: int = 0):
        grants = require_fed(request, "files")
        target = Path(fed_abs_path(path))
        if not target.is_file():
            raise HTTPException(status_code=404, detail="没有这个文件")
        size = target.stat().st_size
        if size > FED_FILE_LIMIT:
            raise HTTPException(status_code=413,
                                detail=f"文件 {size} 字节，超过单次 {FED_FILE_LIMIT} 字节的上限")
        disposition = "inline" if (inline or request.url.path.endswith("/raw")) else "attachment"
        name = target.name or "file"
        return FileResponse(
            str(target),
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''" + quote(name, safe=""),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "X-TmuxWeb-Scopes": " ".join(grants["scopes"]),
            })

    @app.put("/api/fed/files/upload")
    async def fed_upload(request: Request, dir: str = "", name: str = "", offset: int = 0,
                         total: int = 0):
        require_fed(request, "files")
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise HTTPException(status_code=422, detail="invalid file name")
        body = await request.body()
        if len(body) > 16 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="分块上限 16MB")
        directory = fed_abs_path(dir or "~")
        target = Path(directory) / name
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            if total and total > 0:
                with open(target, "r+b" if target.exists() else "wb") as handle:
                    handle.seek(offset)
                    handle.write(body)
                written = target.stat().st_size
                if written < total:
                    return {"status": "partial", "path": str(target), "offset": written,
                            "size": total}
                return {"status": "uploaded", "path": str(target), "size": written}
            target.write_bytes(body)
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "uploaded", "path": str(target), "size": target.stat().st_size}

    @app.post("/api/fed/files/rename")
    async def fed_rename(request: Request, payload: FedFileRequest):
        require_fed(request, "files")
        try:
            moved = file_browser.rename_path(payload.path, payload.new_name)
        except (OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"path": str(moved)}

    @app.post("/api/fed/files/delete")
    async def fed_delete(request: Request, payload: FedFileRequest):
        require_fed(request, "files")
        try:
            removed = file_browser.remove_path(payload.path, payload.recursive)
        except (OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"path": str(removed)}

    @app.post("/api/fed/tokens", status_code=201)
    async def create_fed_token(request: Request, payload: FedTokenRequest):
        """本机登录态才能签发；明文令牌只在这里出现一次。"""
        require_auth(request)
        try:
            issued = await asyncio.to_thread(fed_grants.issue, payload.label, payload.scopes,
                                             payload.panes, payload.ttl)
        except federation.FederationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return issued

    @app.get("/api/fed/tokens")
    async def list_fed_tokens(request: Request):
        require_auth(request)
        return {"tokens": await asyncio.to_thread(fed_grants.listing)}

    @app.delete("/api/fed/tokens/{token_id}")
    async def revoke_fed_token(request: Request, token_id: str):
        require_auth(request)
        if not await asyncio.to_thread(fed_grants.revoke, token_id):
            raise HTTPException(status_code=404, detail="no such token")
        return {"revoked": True, "id": token_id}

    # ---- 联邦（集成端）：记住要拉哪些节点 ---------------------------------
    @app.get("/api/fed/nodes")
    async def list_fed_nodes(request: Request):
        require_auth(request)
        return {"nodes": await asyncio.to_thread(fed_nodes.listing)}

    @app.post("/api/fed/nodes", status_code=201)
    async def add_fed_node(request: Request, payload: FedNodeRequest):
        require_auth(request)
        token, address = federation.parse_auth_key(payload.token)
        base_url = payload.base_url or address
        # 两边都给地址时都要留着：填了内网地址就走直连，key 里带的（可能是 CF 域名）当备用
        fallback_url = payload.fallback_url or (address if base_url != address else "")
        if payload.prefer == "direct" and not payload.base_url and base_url.startswith("https://"):
            # 只有公网地址：问一句对方有没有内网/tailnet 地址。Cloudflare 会把慢链路和
            # 非浏览器 UA 挡在外面（Error 1010），能直连就别绕。
            try:
                info = await asyncio.to_thread(
                    federation.FedClient(base_url, "", token, timeout=6).ping)
            except (federation.FederationError, RuntimeError, OSError, ValueError):
                info = {}
            # tailnet 优先于局域网：云主机上常有一个连不通的 NAT 内网地址（10.0.2.x），
            # 挑错它会让每次请求都先等一次超时
            candidates = sorted(
                (entry for entry in info.get("addresses") or []
                 if entry.get("url") and entry.get("kind") in ("tailnet", "lan")),
                key=lambda entry: 0 if entry["kind"] == "tailnet" else 1)
            if candidates:
                base_url, fallback_url = candidates[0]["url"], base_url
        if not token:
            raise HTTPException(status_code=422, detail="还差 auth key")
        try:
            node = await asyncio.to_thread(fed_nodes.add, payload.label, payload.prefix,
                                           base_url, fallback_url, token, payload.prefer)
        except federation.FederationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return node

    @app.post("/api/fed/nodes/{node_id}/prefer")
    async def prefer_fed_node(request: Request, node_id: str, payload: FedPreferRequest):
        """换首选线路：直连（tailnet/局域网） ↔ Cloudflare。只是换两条地址的顺序。"""
        require_auth(request)
        try:
            node = await asyncio.to_thread(fed_nodes.set_prefer, node_id, payload.prefer)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such node") from exc
        return node

    @app.post("/api/fed/nodes/{node_id}/probe")
    async def probe_fed_node(request: Request, node_id: str):
        """"测试"按钮：现问对方一句，别让用户猜在线状态。"""
        require_auth(request)
        try:
            node = await asyncio.to_thread(fed_nodes.get_node, node_id, True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such node") from exc
        transport = federation.FedTransport(node)
        try:
            info = await asyncio.to_thread(transport.client.ping)
        except (federation.FederationError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # ping 是"通不通"；窗格数量只是附加信息，对方正在等它自己的 SSH 探测时别让按钮转 20 秒
        try:
            count = len(await asyncio.to_thread(lambda: transport.client.panes(timeout=6)))
        except (federation.FederationError, RuntimeError, OSError, ValueError):
            count = None
        return {"ok": True, "host": info.get("host"), "panes": count,
                "prefix": node.get("prefix")}

    @app.delete("/api/fed/nodes/{node_id}")
    async def remove_fed_node(request: Request, node_id: str):
        require_auth(request)
        if not await asyncio.to_thread(fed_nodes.remove, node_id):
            raise HTTPException(status_code=404, detail="no such node")
        return {"removed": True, "id": node_id}

    @app.post("/api/shares", status_code=201)
    async def create_share(request: Request, payload: ShareCreateRequest):
        require_auth(request)
        if not SHARE_SLUG_RE.fullmatch(payload.slug):
            raise HTTPException(status_code=422, detail="slug must use lowercase letters, numbers, and hyphens")
        pane = await asyncio.to_thread(pane_or_404, payload.pane_id)
        transport, actual_pane_id = pane_transport(payload.pane_id)
        try:
            transcript_data = await asyncio.to_thread(transport.transcript, actual_pane_id, 1, pane=pane)
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
        await asyncio.to_thread(validate_shared_session, row)
        transport, actual_pane_id = pane_transport(row["pane_id"])
        resend_state = resend_manager.status(row["pane_id"])
        if not row["session_id"]:
            content = await asyncio.to_thread(transport.capture, actual_pane_id, 2000)
            digest = hashlib.sha1(content.encode()).hexdigest()
            screen_data = {"mode": "screen", "provider": row["provider"] or None, "session_id": None,
                           "mtime": digest, "messages": [], "content": content}
            if resend_state is not None:
                screen_data["resend"] = resend_state
            return screen_data
        data = await asyncio.to_thread(transport.transcript, actual_pane_id, limit)
        data["mode"] = "transcript"
        if resend_state is not None:
            data["resend"] = resend_state
        return data

    @app.post("/share/{slug}/api/input")
    async def share_input(request: Request, slug: str, payload: InputRequest):
        row = get_share(slug)
        require_share_access(request, row)
        if not row["allow_input"]:
            raise HTTPException(status_code=403, detail="this share is read-only")
        await asyncio.to_thread(validate_shared_session, row)
        resend_manager.cancel(row["pane_id"], "superseded")
        transport, actual_pane_id = pane_transport(row["pane_id"])
        submitted = await asyncio.to_thread(transport.send, actual_pane_id, payload.text, payload.submit)
        if submitted is None:
            submitted = True
        if payload.submit and not submitted:
            # The paste landed but Enter never moved it: hand it to the
            # background retry instead of failing it, and keep the 200 -
            # the text did arrive, so a 503 would be a lie.
            status, retrying = retry_or_fail(resend_manager, row["pane_id"], payload.text)
            return {"status": status, "submit": payload.submit,
                    "submitted": False, "retrying": retrying}
        return {"status": "sent", "submit": payload.submit, "submitted": submitted}

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

    @app.get("/assets/theme.css")
    async def theme_stylesheet() -> FileResponse:
        """Theme palettes; they re-point the CSS variables every page already uses."""
        return FileResponse(WEB_DIR / "assets" / "theme.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/theme.js")
    async def theme_script() -> FileResponse:
        """Applies the remembered theme before first paint (no cream flash on a dark theme)."""
        return FileResponse(WEB_DIR / "assets" / "theme.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/recents.css")
    async def recents_stylesheet() -> FileResponse:
        """The shared 「最近」 block: one stylesheet for console, board and grid."""
        return FileResponse(WEB_DIR / "assets" / "recents.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/recents.js")
    async def recents_script() -> FileResponse:
        """The shared 「最近」 block: pins, favourites, rename, height and folding in one place."""
        return FileResponse(WEB_DIR / "assets" / "recents.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/path-links.css")
    async def path_links_stylesheet() -> FileResponse:
        """Chat text that mentions a path: dotted-underline link styling."""
        return FileResponse(WEB_DIR / "assets" / "path-links.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/path-links.js")
    async def path_links_script() -> FileResponse:
        """Chat text that mentions a path: detect, then open dir / file / markdown preview."""
        return FileResponse(WEB_DIR / "assets" / "path-links.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/composer-files.css")
    async def composer_files_stylesheet() -> FileResponse:
        """Chat attachments: one stylesheet for the console composer and the embedded preview."""
        return FileResponse(WEB_DIR / "assets" / "composer-files.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/uploads.js")
    async def uploads_script() -> FileResponse:
        """分块上传 / 断点续传：聊天附件和文件面板共用一份。"""
        return FileResponse(WEB_DIR / "assets" / "uploads.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/composer-files.js")
    async def composer_files_script() -> FileResponse:
        """Chat attachments: upload / thumbnail / delete in one place, shared by both composers."""
        return FileResponse(WEB_DIR / "assets" / "composer-files.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/pane-preview.css")
    async def pane_preview_stylesheet() -> FileResponse:
        """The hover preview panel shared by the home cards and the sidebar rows."""
        return FileResponse(WEB_DIR / "assets" / "pane-preview.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/pane-preview.js")
    async def pane_preview_script() -> FileResponse:
        """TmuxPanePreview — one implementation of "hover a row, see the last message"."""
        return FileResponse(WEB_DIR / "assets" / "pane-preview.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/downloads.css")
    async def downloads_stylesheet() -> FileResponse:
        """Download confirmation sheet (mobile must confirm before downloading)."""
        return FileResponse(WEB_DIR / "assets" / "downloads.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/downloads.js")
    async def downloads_script() -> FileResponse:
        """TmuxDownload.maybe(href, {name, meta}) — ask on touch devices, download directly on desktop."""
        return FileResponse(WEB_DIR / "assets" / "downloads.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/prompt-card.js")
    async def prompt_card_script() -> FileResponse:
        """agent 交互菜单卡片：把 pane 里等着的选择渲染成按钮。"""
        return FileResponse(WEB_DIR / "assets" / "prompt-card.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/markdown-cjk.js")
    async def markdown_cjk_script() -> FileResponse:
        """中文加粗的围栏修正：**任务：**它 这种写法默认不会渲染成粗体。"""
        return FileResponse(WEB_DIR / "assets" / "markdown-cjk.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/upload-cards.css")
    async def upload_cards_style() -> FileResponse:
        """聊天气泡里附件的预览样式。"""
        return FileResponse(WEB_DIR / "assets" / "upload-cards.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/upload-cards.js")
    async def upload_cards_script() -> FileResponse:
        """把 `user uploaded file “a.png”` 换成图片预览/文件片。"""
        return FileResponse(WEB_DIR / "assets" / "upload-cards.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/video-cards.css")
    async def video_cards_style() -> FileResponse:
        """视频卡片样式：聊天页和看板共用一份。"""
        return FileResponse(WEB_DIR / "assets" / "video-cards.css", media_type="text/css",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/video-cards.js")
    async def video_cards_script() -> FileResponse:
        """聊天里把视频链接变成就播放器（preload=metadata，不点不下载）。"""
        return FileResponse(WEB_DIR / "assets" / "video-cards.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/transfer.js")
    async def transfer_script() -> FileResponse:
        """直连传输：探测内网/Tailscale 源站、拿票据、把字节搬过去（拿不到就回落 CF）。"""
        return FileResponse(WEB_DIR / "assets" / "transfer.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/plugins-ui.js")
    async def plugins_ui_script():
        """Sidebar buttons contributed by plugins; no-store so a plugin edit shows up on reload."""
        return FileResponse(WEB_DIR / "assets" / "plugins-ui.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/settings.js")
    async def settings_asset() -> FileResponse:
        """The settings panel shared by the console and /grid."""
        return FileResponse(WEB_DIR / "assets" / "settings.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/assets/console.bundle.js")
    async def console_bundle() -> Response:
        """The console scripts as one bundle, built once at startup.

        Served with no-store unless the request carries the content-hash ?v=
        (injected into index.html at serve time); a versioned URL is immutable
        via the versioned_asset_cache middleware. theme.js is deliberately not
        in the bundle: it must run synchronously before first paint.
        """
        return Response(content=CONSOLE_BUNDLE_BYTES, media_type="application/javascript",
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
            panes = [pane_metadata(pane) for pane in await asyncio.to_thread(tmux.list_panes)]
            numbers = await asyncio.to_thread(home.ensure_numbers, [pane["id"] for pane in panes])
            for pane in panes:
                if pane["id"] in numbers:
                    pane["number"] = numbers[pane["id"]]
            return {"panes": panes}
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

    def number_panes(payload):
        """给每台的每个 pane 补上短编号（跨主机唯一，首次见到时分配）。

        接受 `[...hosts]` 或 `{"hosts": [...]}`，返回同样的形状 —— 快照里存的是后者。
        """
        hosts = payload.get("hosts") if isinstance(payload, dict) else payload
        pane_ids = [pane.get("id") for host in hosts for pane in (host.get("panes") or [])
                    if pane.get("id") and host.get("kind") != "fed"]
        numbers = home.ensure_numbers(pane_ids)
        for host in hosts:
            for pane in host.get("panes") or []:
                number = numbers.get(pane.get("id"))
                if number:
                    pane["number"] = number
        return payload

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
                return number_panes(refreshed)
        if cached is not None and not refresh and (time.time() - hosts_snapshot["at"]) < hosts_snapshot["ttl"]:
            return number_panes(cached)
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

        fed_public = []
        for node in fed_nodes.listing():
            if not node.get("enabled", True):
                continue
            fed_public.append({"id": node["id"], "name": node.get("label") or node["prefix"],
                               "kind": "fed", "prefix": node.get("prefix"),
                               "url": node.get("base_url") or node.get("fallback_url"),
                               "status": "offline", "panes": []})

        async def load_fed(public: dict) -> None:
            node = await asyncio.to_thread(fed_nodes.get_node, public["id"], True)
            federated = federation.FedTransport(node)
            try:
                panes = await asyncio.to_thread(federated.list_panes)
            except (federation.FederationError, RuntimeError, OSError) as exc:
                public["error"] = str(exc)
                return
            public["panes"] = [pane | {"prefix": node.get("prefix")} for pane in panes]
            public["status"] = "online"

        await asyncio.gather(*(load_fed(item) for item in fed_public))
        for host in fed_public:
            for pane in host.get("panes") or []:
                # 编号沿用对端的，只有前缀区分；本机号段不参与
                pane["number"] = pane.get("number")
                pane["prefix"] = host.get("prefix")
        hosts.extend(fed_public)
        payload = {"hosts": number_panes(hosts)}      # 短编号：跨主机唯一的 #N
        hosts_snapshot["value"] = payload
        hosts_snapshot["at"] = time.time()
        return payload

    async def perform_mcp_tool(name: str, arguments: dict) -> dict:
        """Same semantics as mcp_relay.py, but executed in-process (no python3 on the client)."""
        arguments = arguments or {}
        if plugin_manager.get("mgr") and name in plugin_manager["mgr"].tools:
            try:
                result = await plugin_manager["mgr"].call_tool(name, arguments)
            except KeyError:
                pass
            except PluginError as exc:
                raise McpToolError(str(exc)) from exc
            else:
                return result if isinstance(result, dict) else {"result": result}
        if name == "run_command":
            command = str(arguments.get("command") or "").strip()
            if not command:
                raise McpToolError("command must not be empty")
            if len(command) > 20000:
                raise McpToolError("command is longer than 20000 characters")
            if (os.environ.get("TMUX_WEB_EXEC") or "on").strip().lower() in {"0", "off", "false", "no"}:
                raise McpToolError("exec 已被 TMUX_WEB_EXEC=off 关闭")
            timeout = max(1, min(int(arguments.get("timeout") or 60), 600))
            return await run_exec(str(arguments.get("host") or "").strip(), command,
                                  str(arguments.get("cwd") or "").strip(), timeout,
                                  max(1000, min(int(arguments.get("max_output") or 200000), 2000000)))
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
            numbers = await asyncio.to_thread(
                home.ensure_numbers, [peer["id"] for peer in peers if peer.get("id")])
            return {"agents": [
                {
                    "target": peer["id"],
                    "number": numbers.get(peer["id"]),
                    # 联邦 pane 的编号要带前缀：ddr#12（两边各自的 #N 会撞号）
                    "handle": (f"{fed_prefix_of(peer.get('host_id') or '')}#{numbers.get(peer['id'])}"
                               if numbers.get(peer["id"]) and fed_prefix_of(peer.get("host_id") or "")
                               else (f"#{numbers.get(peer['id'])}" if numbers.get(peer["id"]) else peer["id"])),
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
            plugin_tools = plugin_manager["mgr"].tool_schemas() if plugin_manager.get("mgr") else []
            return reply({"tools": mcp_relay.tool_schemas() + plugin_tools})
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

    AGENT_CACHE: dict[str, tuple[float, list]] = {}
    AGENT_CACHE_TTL = 300.0

    async def agents_for(host_id: str, refresh: bool = False) -> list[dict]:
        """What a host can start, cached: probing costs one ssh round trip."""
        key = host_id or "localhost"
        cached = AGENT_CACHE.get(key)
        if cached and not refresh and (time.monotonic() - cached[0]) < AGENT_CACHE_TTL:
            return [dict(item) for item in cached[1]]
        if key in ("", "localhost"):
            found = await asyncio.to_thread(agent_catalog.probe_local)
        else:
            try:
                host = remote_hosts.get_host(key, include_credential=True)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="remote host not found") from exc
            found = await asyncio.to_thread(lambda: runner_for(host).probe_agents())
        AGENT_CACHE[key] = (time.monotonic(), found)
        return [dict(item) for item in found]

    @app.get("/api/hosts/{host_id}/agents")
    async def list_host_agents(request: Request, host_id: str, refresh: bool = False):
        require_auth(request)
        agents = await agents_for(host_id, refresh=refresh)
        return {"host": host_id, "agents": agents}

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request, payload: SessionCreateRequest):
        """Create a detached tmux session on the local host or a remote one.

        ``agent`` is optional: when given (and installed on that host) the session
        starts with the agent instead of a bare shell, with ``agent_args`` passed
        through as extra argv.
        """
        require_auth(request)
        if payload.host in ("", "localhost"):
            transport, host_id = tmux, "localhost"
        else:
            try:
                host = remote_hosts.get_host(payload.host, include_credential=True)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="remote host not found") from exc
            transport, host_id = runner_for(host), payload.host
        command = None
        if (payload.agent or "").strip():
            installed = {item["name"]: item for item in await agents_for(host_id)}
            wanted = (payload.agent or "").strip().lower()
            if wanted not in installed:
                raise HTTPException(
                    status_code=422,
                    detail="agent not installed on this host: %s" % wanted)
            try:
                # 绝对路径：tmux pane 的 PATH 不一定和 ssh 探测时看到的一样
                command = agent_catalog.command_for(payload.agent, payload.agent_args,
                                                    binary=installed[wanted].get("path", ""))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            created = await asyncio.to_thread(
                transport.create_session, payload.name, payload.path, command,
                payload.create_dir, payload.keep_open)
            panes = await asyncio.to_thread(transport.list_panes)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            if "already exists" in message or "duplicate" in message:
                status = 409
            elif any(marker in message for marker in
                     ("no such directory", "cannot create directory", "not a directory",
                      "session name", "agent")):
                status = 422
            else:
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
        if fresh:
            # 新 pane 立刻拿号：侧栏/MCP 都不用等下一次全量刷新
            numbers = await asyncio.to_thread(home.ensure_numbers, [pane["id"] for pane in fresh])
            for pane in fresh:
                number = numbers.get(pane["id"])
                if number:
                    pane["number"] = number
                    pane["handle"] = f"#{number}"
        return {"session": created, "host": host_id, "panes": fresh}

    @app.delete("/api/panes/{pane_id}")
    async def delete_pane(request: Request, pane_id: str):
        """Close one pane only - the session stays if it has other panes."""
        require_auth(request)
        try:
            transport, actual = pane_transport(pane_id)
        except HTTPException:
            raise
        try:
            killed = await asyncio.to_thread(transport.kill_pane, actual)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            raise HTTPException(status_code=404 if "no such pane" in message else 503,
                                detail=message) from exc
        return killed

    @app.delete("/api/sessions/{name}")
    async def delete_session(request: Request, name: str, host: str = "localhost"):
        """Close one tmux session (all of its windows and panes)."""
        require_auth(request)
        if host in ("", "localhost"):
            transport, host_id = tmux, "localhost"
        else:
            try:
                record = remote_hosts.get_host(host, include_credential=True)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="remote host not found") from exc
            transport, host_id = runner_for(record), host
        try:
            killed = await asyncio.to_thread(transport.kill_session, name)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            status = 404 if "no such session" in message else 422
            if status == 422 and "session name" not in message:
                status = 503
            raise HTTPException(status_code=status, detail=message) from exc
        return {"session": killed.get("session", name), "host": host_id, "killed": True}

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
        if payload.transport not in {"ssh", "agent"}:
            raise HTTPException(status_code=422, detail="transport must be ssh or agent")
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
                jump_spec=payload.jump_spec, transport=payload.transport,
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
            # check + list_panes 曾经各按自己的超时串行等：两台跳板机都慢时一次
            # 验证能拖 27s 以上，Cloudflare 100s 上限内再叠加别的慢步骤就 502。
            # 给整次验证一个总预算，剩多少时间 list_panes 就用多少，绝不超支。
            budget = float(os.environ.get("TMUX_WEB_HOST_TEST_BUDGET", "25"))
            started = time.monotonic()
            banner = await asyncio.to_thread(runner.check, timeout=max(1, min(12, int(budget))))
            remaining = budget - (time.monotonic() - started)
            panes = await asyncio.to_thread(runner.list_panes, timeout=max(1, int(remaining)))
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
        if payload.transport is not None and payload.transport not in {"ssh", "agent"}:
            raise HTTPException(status_code=422, detail="transport must be ssh or agent")
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
                transport=payload.transport,
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
                transport=previous.get("transport", "ssh"),
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
                "enabled", "position", "jump_host_id", "jump_spec", "tmux_path", "transport",
            )}
            item["transport"] = item.get("transport") or "ssh"   # hosts stored before the column existed
            status = agent_status_provider(host["id"]) if agent_status_provider else {}
            item["agent_online"] = bool(status.get("agent_online", False))
            item["agent_last_seen"] = status.get("agent_last_seen")
            item["last_transport"] = status.get("last_transport")
            item["last_transport_at"] = status.get("last_transport_at")
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
        transport, actual_pane_id = pane_transport(pane_id)
        batched = getattr(transport, "capture_with_pane", None)
        try:
            if batched is not None:
                # One agent round trip: list + capture in a single batch.
                # Anything without the batched path keeps the two-step flow.
                _, content = await asyncio.to_thread(
                    batched, actual_pane_id, history)
            else:
                pane = await asyncio.to_thread(pane_or_404, pane_id)
                content = await asyncio.to_thread(
                    transport.capture, actual_pane_id, history, pane=pane)
            return {"pane": pane_id, "content": content}
        except KeyError:
            raise HTTPException(status_code=404, detail="remote pane not found") from None
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Last full transcript read per local pane: the session file the messages
    # came from, its stat, and the signature the client already holds. Lets an
    # unchanged poll skip the read+parse entirely (see transcript()).
    transcript_sig_cache: dict[str, dict] = {}

    def waiting_facts(messages):
        """(last role, last user timestamp) — what the waiting verdict is built on."""
        last = messages[-1] if messages else {}
        last_user_ts = None
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user_ts = message.get("timestamp")
                break
        return (last.get("role") or "", last_user_ts)

    def agent_busy(provider: str, pane: dict | None) -> bool:
        """True when the pane's own session file says its turn is still running.

        One tail read (<=64KB) plus the session-file lookup, no tmux capture
        and no shell out — the same probe the state sweep already makes.
        Remote panes never reach here: their session files are not local.
        """
        if not pane or pane.get("host_id") not in (None, "localhost") or not provider:
            return False
        try:
            return agent_status.status_for_pane(
                provider, pane.get("path", ""), pane.get("title", "")) == agent_status.BUSY
        except Exception:  # noqa: BLE001 - a failed probe must not fail the transcript
            return False

    def with_waiting_hint(data: dict, pane: dict | None = None) -> dict:
        """Tell the UI whether the agent is still working on the last message.

        A trailing user message means the turn has not been answered yet. But
        the first assistant reply does not end the turn: a long task keeps
        streaming after it, so when the last message is an assistant one the
        session file gets the final say — BUSY there means still waiting.
        ``waiting_since`` is the turn's start (the last user message), so the
        progress clock does not restart on every partial reply.
        """
        messages = data.get("messages") or []
        last_role, last_user_ts = waiting_facts(messages)
        waiting = (last_role == "user")
        if not waiting and pane is not None:
            provider = (data.get("provider") or pane.get("provider")
                        or transcript_reader.provider_for_command(pane.get("command")) or "")
            waiting = agent_busy(provider, pane)
        data["waiting"] = bool(waiting)
        data["waiting_since"] = last_user_ts if waiting else None
        return data

    def fresh_waiting_for_hit(pane: dict, hit: dict) -> tuple[bool, object]:
        """Re-judge waiting for a stat-unchanged stub.

        The messages are the same as when the cache entry was written, so the
        last-role rule still holds — but the agent may have started (or
        finished) since, which only the session file knows. A cached waiting
        must never be served stale, or the poll interval decays to 10s while
        a long task is still silently running.
        """
        waiting = (hit.get("last_role") == "user")
        if not waiting:
            provider = (pane.get("provider")
                        or transcript_reader.provider_for_command(pane.get("command")) or "")
            waiting = agent_busy(provider, pane)
        return bool(waiting), (hit.get("last_user_ts") if waiting else None)

    def unchanged_stub(pane: dict, hit: dict, signature: str, long_poll: bool = False,
                       resend: dict | None = None) -> dict:
        """The tiny unchanged reply, with waiting re-judged and the cache updated."""
        waiting, since = fresh_waiting_for_hit(pane, hit)
        hit["waiting"] = waiting
        hit["waiting_since"] = since
        stub: dict = {"unchanged": True, "signature": signature, "waiting": waiting,
                      "waiting_since": since, "has_more": hit["has_more"]}
        if resend is not None:
            stub["resend"] = resend
        if long_poll:
            stub["long_poll"] = True
        return stub

    async def await_transcript_change(pane: dict, hit: dict, wait_seconds: float) -> str:
        """Hang until the session file moves, waiting flips, or the deadline.

        Returns "changed", "waiting_flipped" or "timeout". The caller already
        checked that the client's baseline matches this cache entry. Only
        stat() and the cheap session-file status probe run here; the transcript
        itself is never re-read inside the loop, and the loop only ever sleeps
        with asyncio.sleep, so the event loop stays free for other requests.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        last_waiting_check = 0.0
        while True:
            try:
                stat = os.stat(hit["path"])
            except OSError:
                return "changed"
            if (stat.st_mtime_ns, stat.st_size) != (hit["mtime_ns"], hit["size"]):
                return "changed"
            now = loop.time()
            if now - last_waiting_check >= LONG_POLL_WAITING_CHECK_SECONDS:
                last_waiting_check = now
                waiting, _since = await asyncio.to_thread(fresh_waiting_for_hit, pane, hit)
                if waiting != hit["waiting"]:
                    return "waiting_flipped"
            if now >= deadline:
                return "timeout"
            await asyncio.sleep(min(LONG_POLL_CHECK_SECONDS, max(0.0, deadline - now)))

    @app.get("/api/panes/{pane_id}/transcript")
    async def transcript(request: Request, pane_id: str, limit: int = 200, refresh: int = 0,
                         sig: str = "", since: str = "", wait: float = 0):
        """Structured messages; ``sig`` lets an unchanged poll return a stub.

        The console polls this while a turn runs.  Sending 300 messages of JSON
        every 2s is the single biggest waste in the chat view, so the client
        echoes the signature it already has and gets ``{"unchanged": true}``
        (a few dozen bytes) when nothing moved.  When the session file's stat
        is unchanged since the last full read, the stub is answered without
        re-reading or re-parsing anything at all.

        ``since`` (last message id, or a full signature) asks for only the
        messages after that point; an unknown anchor falls back to the full
        list. ``wait`` (seconds, clamped to 0..25) long-polls instead: while
        the baseline still matches, the request hangs until the session file
        moves, the agent's running state flips, or the deadline passes.
        ``refresh=1`` never hangs — a manual refresh answers immediately.
        Remote panes keep the old behaviour for waiting (no local session
        file to probe, no stat to wait on); ``since`` still slices their
        already-read messages.
        """
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        # The background retry state rides the same poll the console already
        # makes.  The field is only attached while a record exists for this
        # pane, so an idle pane's responses stay byte-identical.
        resend_state = resend_manager.status(pane_id)
        is_local = pane.get("host_id") in (None, "localhost")
        wait_seconds = 0.0 if refresh else clamp_long_poll_wait(wait)
        if not refresh and is_local:
            hit = transcript_sig_cache.get(pane_id)
            if hit:
                try:
                    stat = os.stat(hit["path"])
                except OSError:
                    stat = None
                if stat is not None and (stat.st_mtime_ns, stat.st_size) == (hit["mtime_ns"], hit["size"]):
                    baseline = False
                    if sig and sig == hit["signature"]:
                        baseline = True
                    if since:
                        anchor = since.rsplit("|", 1)[-1] if "|" in since else since
                        if anchor and anchor == hit["signature"].rsplit("|", 1)[-1]:
                            baseline = True
                    if baseline:
                        if wait_seconds > 0:
                            outcome = await await_transcript_change(pane, hit, wait_seconds)
                            if outcome in ("waiting_flipped", "timeout"):
                                return unchanged_stub(pane, hit, hit["signature"], long_poll=True,
                                                      resend=resend_state)
                            # "changed": fall through to the full read below
                        else:
                            return unchanged_stub(pane, hit, sig or hit["signature"],
                                                  resend=resend_state)
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            if refresh and hasattr(transport, "transcript"):
                data = await asyncio.to_thread(transport.transcript, actual_pane_id, limit, True, pane=pane)
            else:
                data = await asyncio.to_thread(transport.transcript, actual_pane_id, limit, pane=pane)
            data = with_waiting_hint(data, pane if is_local else None)
            messages = data.get("messages") or []
            signature = "%s|%s|%s" % (data.get("mtime"), len(messages),
                                     messages[-1].get("id") if messages else "")
            last_role, last_user_ts = waiting_facts(messages)
            source_path = data.get("path")
            if source_path and is_local:
                try:
                    stat = os.stat(source_path)
                except OSError:
                    stat = None
                if stat is not None:
                    transcript_sig_cache[pane_id] = {
                        "path": source_path, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size,
                        "signature": signature, "waiting": data.get("waiting"),
                        "waiting_since": data.get("waiting_since"), "has_more": data.get("has_more"),
                        "last_role": last_role, "last_user_ts": last_user_ts}
                    if len(transcript_sig_cache) > 512:
                        transcript_sig_cache.clear()
                else:
                    transcript_sig_cache.pop(pane_id, None)
            held = bool(wait_seconds > 0 and is_local)
            if sig and sig == signature:
                stub: dict = {"unchanged": True, "signature": signature, "waiting": data.get("waiting"),
                              "waiting_since": data.get("waiting_since"), "has_more": data.get("has_more")}
                if resend_state is not None:
                    stub["resend"] = resend_state
                if held:
                    stub["long_poll"] = True
                return stub
            if since and not refresh:
                anchor = since.rsplit("|", 1)[-1] if "|" in since else since
                ids = [str(item.get("id")) for item in messages]
                if anchor in ids:
                    index = ids.index(anchor)
                    if index == len(ids) - 1:
                        stub = {"unchanged": True, "signature": signature,
                                "waiting": data.get("waiting"),
                                "waiting_since": data.get("waiting_since"),
                                "has_more": data.get("has_more")}
                        if resend_state is not None:
                            stub["resend"] = resend_state
                        if held:
                            stub["long_poll"] = True
                        return stub
                    incremental = dict(data)
                    incremental["messages"] = messages[index + 1:]
                    incremental["signature"] = signature
                    incremental["incremental"] = True
                    if resend_state is not None:
                        incremental["resend"] = resend_state
                    if held:
                        incremental["long_poll"] = True
                    return incremental
                # anchor not in the current window (rotation, or older than the
                # limit): fall through to the full list below
            data["signature"] = signature
            if resend_state is not None:
                data["resend"] = resend_state
            if held:
                data["long_poll"] = True
            return data
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/panes/{pane_id}/input")
    async def send_input(request: Request, pane_id: str, payload: InputRequest):
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        try:
            # A new send supersedes any background retry still running for
            # this pane, so its Enter can never land in this paste.
            resend_manager.cancel(pane_id, "superseded")
            transport, actual_pane_id = pane_transport(pane_id)
            submitted = await asyncio.to_thread(transport.send, actual_pane_id, payload.text, payload.submit, pane=pane)
            if submitted is None:
                submitted = True
            if payload.submit and not submitted:
                # The paste landed but Enter never moved it: hand it to the
                # background retry instead of failing it, and keep the 200 -
                # the text did arrive, so a 503 would be a lie.
                status, retrying = retry_or_fail(resend_manager, pane_id, payload.text)
                return {"status": status, "pane": pane_id, "submit": payload.submit,
                        "submitted": False, "retrying": retrying}
            return {"status": "sent", "pane": pane_id, "submit": payload.submit, "submitted": submitted}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/panes/{pane_id}/models")
    async def pane_models(request: Request, pane_id: str):
        """Which model this pane runs now, and what a switch would send."""
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        provider = pane_provider(pane)
        if not agent_models.supported(provider):
            raise HTTPException(status_code=422, detail="this pane is not an agent with a model picker")
        current, current_data = "", {}
        try:
            transport, actual = pane_transport(pane_id)
            current_data = await asyncio.to_thread(transport.transcript, actual, 1, pane) or {}
            current = current_data.get("model") or ""
        except (HTTPException, TmuxError, RuntimeError, OSError, ValueError):
            current = ""
        configured = (current_data.get("model_provider") or "") if isinstance(current_data, dict) else ""
        remote = pane.get("host_id") not in (None, "localhost")
        if not configured and provider == "hermes" and not remote:
            configured = agent_models.hermes_route()
        presets = agent_models.preset_for(provider)
        groups = []
        if provider == "hermes":
            # Hermes 的"可选模型"就是各 provider 的 /v1/models；config 里通常不止一组
            try:
                runner = None
                if remote:
                    runner, _ = pane_transport(pane_id)
                    if not hasattr(runner, "run"):
                        runner = None
                groups = await asyncio.to_thread(agent_models.hermes_groups, None, runner, configured)
            except (HTTPException, RuntimeError, OSError, ValueError):
                groups = []
            if groups:
                presets = list(groups[0]["models"])
        return {"pane": pane_id, "provider": provider, "current": current,
                "provider_label": agent_models.provider_label(provider, configured),
                "current_provider": configured,
                "presets": presets,
                "groups": groups,
                "picker_only": agent_models.picker_only(provider),
                "template": agent_models.switch_command(provider, "MODEL").replace("MODEL", "{model}")}

    @app.post("/api/panes/{pane_id}/model")
    async def switch_pane_model(request: Request, pane_id: str, payload: ModelSwitchRequest):
        """Type the agent's own ``/model <id>`` into the pane."""
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        provider = pane_provider(pane)
        try:
            command = agent_models.switch_command(
                provider, payload.model,
                payload.provider if provider == "hermes" else "")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            transport, actual = pane_transport(pane_id)
            await asyncio.to_thread(transport.send, actual, command, True, pane=pane)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "sent", "pane": pane_id, "provider": provider, "model": payload.model,
                "command": command}

    @app.post("/api/panes/{pane_id}/interrupt")
    async def interrupt_pane(request: Request, pane_id: str):
        """Stop the turn that is running in this pane, using the key its CLI expects."""
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
        key = interrupt_key(provider)
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            await asyncio.to_thread(transport.send_key, actual_pane_id, key, pane=pane)
            return {"status": "sent", "pane": pane_id, "key": key, "provider": provider}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/panes/{pane_id}/key")
    async def send_key(request: Request, pane_id: str, payload: KeyRequest):
        """Send one named key (Escape interrupts a running agent turn)."""
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        if payload.key not in ALLOWED_KEYS:
            raise HTTPException(status_code=422, detail="unsupported key")
        try:
            transport, actual_pane_id = pane_transport(pane_id)
            await asyncio.to_thread(transport.send_key, actual_pane_id, payload.key, pane=pane)
            return {"status": "sent", "pane": pane_id, "key": payload.key}
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # 上一次解析出来的提示（GET /prompt 会填）：点按钮时直接用，省一次抓屏。
    # 远端 pane 一次抓屏就是一个 SSH 往返，省掉它点击才不卡。
    prompt_cache: dict[str, tuple[float, dict]] = {}
    prompt_cache_lock = asyncio.Lock()

    async def remember_prompt(pane_id: str, found: dict | None) -> None:
        async with prompt_cache_lock:
            if found:
                prompt_cache[pane_id] = (time.monotonic(), found)
            else:
                prompt_cache.pop(pane_id, None)

    async def cached_prompt(pane_id: str, max_age: float = 15.0) -> dict | None:
        async with prompt_cache_lock:
            entry = prompt_cache.get(pane_id)
            if entry and time.monotonic() - entry[0] <= max_age:
                return entry[1]
            return None

    async def read_prompt(pane_id: str) -> tuple[dict | None, str]:
        """抓一次屏幕并解析出等待中的菜单；认不出就是 (None, screen)。"""
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        transport, actual = pane_transport(pane_id)
        text = await asyncio.to_thread(transport.capture, actual, 80, pane=pane,
                                       visible_only=True)
        found = agent_prompts.parse_screen(text)
        await remember_prompt(pane_id, found)
        return found, text

    def keys_are_safe(keys: list[str]) -> bool:
        for key in keys:
            if key in ALLOWED_KEYS:
                continue
            if len(key) == 1 and (key.isalnum() or key in "\r"):
                continue
            return False
        return True

    @app.get("/api/panes/{pane_id}/prompt")
    async def pane_prompt(request: Request, pane_id: str):
        """屏幕上正在等的选择（kimi 的模型菜单、claude 的权限确认…）。

        认不出就返回 ``{"prompt": null}`` —— 前端据此不显示按钮，用户照旧用终端。
        """
        require_auth(request)
        if home.get_prefs().get("prompt_card", "off") != "on":
            # 实验性功能：设置里没打开就当屏幕上没有等待中的选择
            return {"pane": pane_id, "prompt": None, "screen": [], "disabled": True}
        try:
            found, text = await read_prompt(pane_id)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        lines = (text or "").splitlines()
        while lines and not lines[-1].strip():      # TUI 下方常留空白，别把它当"屏幕原文"
            lines.pop()
        return {"pane": pane_id, "prompt": found, "screen": lines[-14:] if found else []}

    @app.post("/api/panes/{pane_id}/prompt/choose")
    async def pane_prompt_choose(request: Request, pane_id: str, payload: PromptChoiceRequest):
        """把"选第 N 项"变成按键发进 pane，再抓屏复核。

        位置式的列表不能发数字（kimi 的输入框会把数字当搜索词），所以按键序列
        由 ``agent_prompts.choice_keys`` 生成：顶到第一项，再走 N 步，最后 Enter。
        """
        require_auth(request)
        if home.get_prefs().get("prompt_card", "off") != "on":
            raise HTTPException(status_code=409, detail="交互菜单卡片已在设置里关闭")
        try:
            found = await cached_prompt(pane_id)
            if not found:
                found, _text = await read_prompt(pane_id)
            if not found:
                raise HTTPException(status_code=409, detail="屏幕上没有等待中的选择")
            if payload.index is not None and not found.get("options"):
                raise HTTPException(status_code=422,
                                    detail="这个界面只有一个开关，请直接点分段或按 Enter")
            if payload.toggle is not None and payload.value is not None:
                keys = agent_prompts.toggle_keys(found, payload.toggle, payload.value)
                if not keys:
                    return {"ok": True, "prompt": found, "unchanged": True}
            elif payload.index is not None:
                keys = agent_prompts.choice_keys(found, payload.index)
            else:
                keys = agent_prompts.choice_for_key(found, payload.key)
                if keys is None:
                    raise HTTPException(status_code=422, detail="unsupported key")
            if not keys_are_safe(keys):
                raise HTTPException(status_code=422, detail="unsupported key")
            pane = await asyncio.to_thread(pane_or_404, pane_id)
            transport, actual = pane_transport(pane_id)
            send_many = getattr(transport, "send_keys", None)
            if send_many is not None:
                await asyncio.to_thread(send_many, actual, keys, pane=pane)
            else:
                for key in keys:
                    await asyncio.to_thread(transport.send_key, actual, key, pane=pane)
                    await asyncio.sleep(0.06)
            # 发完立刻返回：复核交给前端（它本来就在轮询 /prompt）。
            # 以前在这里串行抓屏 5 次，远端每抓一次就是一个 SSH 往返，点击要卡好几秒。
            async with prompt_cache_lock:
                prompt_cache.pop(pane_id, None)      # 缓存作废，下一次轮询拿新状态
            return {"ok": True, "sent": keys, "pending": True, "prompt": found}
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def file_target(pane_ref: str) -> tuple[object, str, dict]:
        # blocking pane lookup stays off the event loop; the transport itself
        # does no I/O, so only the lookup moves to a thread
        pane = await asyncio.to_thread(pane_or_404, pane_ref)
        transport, actual_pane_id = pane_transport(pane_ref)
        return transport, actual_pane_id, pane

    @app.get("/api/panes/{pane_id}/tail")
    async def pane_tail(request: Request, pane_id: str, lines: int = 10):
        """最后几行屏幕文字：agent 还在跑的时候，聊天里先看到"它在干什么"。

        比 /transcript 便宜得多（一次 capture），而且只返回纯文本，客户端按 digest
        判断要不要重绘——空闲时每 1.5 秒一次抓屏，在远端就是一次 ssh 往返。
        """
        require_auth(request)
        pane = await asyncio.to_thread(pane_or_404, pane_id)
        try:
            transport, actual = pane_transport(pane_id)
            raw = await asyncio.to_thread(transport.capture, actual, max(3, min(lines, 40)), pane=pane)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        text = "\n".join(line.rstrip() for line in (raw or "").splitlines() if line.strip())
        tail = "\n".join(text.splitlines()[-max(3, min(lines, 40)):])
        return {"digest": hashlib.sha1(tail.encode()).hexdigest(), "text": tail}

    @app.get("/api/panes/{pane_id}/files")
    async def list_pane_files(request: Request, pane_id: str, path: str = "", hidden: int = 0,
                              limit: int = 2000):
        require_auth(request)
        try:
            transport, _actual, pane = await file_target(pane_id)
            directory = path or pane.get("path") or "."
            data = await asyncio.to_thread(transport.list_files, directory, bool(hidden),
                                           max(1, min(limit, 5000)))
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        data["pane"] = pane_id
        return data

    STAT_CACHE: dict[tuple, tuple[float, dict]] = {}
    STAT_CACHE_TTL = 60.0
    STAT_CACHE_MAX = 512

    def remember_stat(key: tuple, answer: dict) -> None:
        """记住一次 stat 结果：悬停/点击不该每次都付一次 SSH 往返，深层搜索尤其贵。"""
        if len(STAT_CACHE) >= STAT_CACHE_MAX:
            oldest = min(STAT_CACHE, key=lambda item: STAT_CACHE[item][0])
            STAT_CACHE.pop(oldest, None)
        STAT_CACHE[key] = (time.monotonic(), copy.deepcopy(answer))

    @app.get("/api/panes/{pane_id}/files/stat")
    async def stat_pane_path(request: Request, pane_id: str, path: str = ""):
        """Is this path a file or a directory? Used by chat text that mentions paths.

        Agents often print paths relative to *their* cwd, e.g. ``ate/s3k/note.md``
        while the pane sits one or two levels elsewhere. So a relative path is
        probed against several bases (pane cwd → its parent → home) and the first
        hit wins; the answer always carries the absolute ``path`` plus
        ``offset`` (which base matched, 0 = as typed).
        """
        require_auth(request)
        if not path.strip():
            raise HTTPException(status_code=422, detail="path is required")
        try:
            transport, _actual, pane = await file_target(pane_id)
            base = (pane.get("path") or "").strip()
            cache_key = (pane_id, base, path.strip())
            hit_cache = STAT_CACHE.get(cache_key)
            if hit_cache and time.monotonic() - hit_cache[0] < STAT_CACHE_TTL:
                return copy.deepcopy(hit_cache[1])
            raw = os.path.expanduser(path.strip())
            if os.path.isabs(raw):
                candidates = [os.path.normpath(raw)]
            else:
                bases = [base or ".", os.path.dirname(base.rstrip("/")) if base else "",
                         os.path.expanduser("~")]
                candidates = []
                for directory in bases:
                    if not directory:
                        continue
                    candidate = os.path.normpath(os.path.join(directory, raw))
                    if candidate not in candidates:
                        candidates.append(candidate)
            found = None
            parent = ""
            for index, candidate in enumerate(candidates[:4]):
                folder = os.path.dirname(candidate.rstrip("/")) or candidate
                try:
                    listing = await asyncio.to_thread(transport.list_files, folder, True, 5000)
                except (TmuxError, RuntimeError, OSError, ValueError, file_browser.FileBrowserError):
                    continue        # 这一层的父目录都不存在，换下一个基准
                name = os.path.basename(candidate.rstrip("/"))
                for entry in listing.get("entries") or []:
                    if entry.get("name") == name:
                        found = (candidate, folder, entry, index)
                        break
                if found:
                    break
            # 更鲁棒的一步：agent 报的路径可能相对它自己的子目录（cwd=runs/195 时说
            # ate/s3k/x.md），前端也可能已经把它拼成了「旧 cwd + 相对路径」的绝对路径。
            # 两种都归一成 (从哪里找, 找什么后缀) 再搜。
            searches: list[tuple[str, str]] = []
            if os.path.isabs(raw):
                base_dir = base or os.path.dirname(raw)
                prefix = base_dir.rstrip("/") + "/"
                if base_dir and raw.startswith(prefix):
                    searches.append((base_dir, os.path.relpath(raw, base_dir)))
                else:
                    tail = "/".join([part for part in raw.split("/") if part][-2:])
                    if tail.count("/") >= 1:
                        searches.append((base_dir or "/", tail))
            else:
                searches = [(item, raw) for item in bases if item][:2]
            if not found and any(suffix.count("/") >= 1 for _anchor, suffix in searches):
                for anchor, suffix in searches:
                    if suffix.count("/") < 1:
                        continue
                    hits = await asyncio.to_thread(transport.find_suffix, anchor, suffix)
                    if not hits:
                        continue
                    best = hits[0]
                    folder = os.path.dirname(best) or "."
                    kind = "dir" if os.path.isdir(best) else "file"
                    try:
                        stat = await asyncio.to_thread(transport.list_files, folder, True, 5000)
                        entry = next((e for e in (stat.get("entries") or [])
                                      if e.get("name") == os.path.basename(best)), None)
                    except (TmuxError, RuntimeError, OSError, ValueError,
                            file_browser.FileBrowserError):
                        entry = None
                    answer = {"path": best, "name": os.path.basename(best.rstrip("/")), "exists": True,
                            "kind": (entry or {}).get("type") or kind,
                            "size": (entry or {}).get("size") or 0,
                            "mtime": (entry or {}).get("mtime") or 0,
                            "parent": folder, "offset": 0, "via": "search",
                            "relative": os.path.relpath(best, base or ".")}
                    remember_stat(cache_key, answer)
                    return answer
            if not found:
                wanted = candidates[0]
                name = os.path.basename(wanted.rstrip("/"))
                answer = {"path": wanted, "name": name, "exists": False, "kind": None,
                          "size": 0, "mtime": 0,
                          "parent": os.path.dirname(wanted.rstrip("/")) or wanted, "offset": 0,
                          "via": "none"}
                remember_stat(cache_key, answer)
                return answer
            wanted, parent, entry, index = found
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError, file_browser.FileBrowserError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        answer = {"path": wanted, "name": os.path.basename(wanted.rstrip("/")), "exists": True,
                  "kind": entry.get("type"), "size": entry.get("size") or 0,
                  "mtime": entry.get("mtime") or 0, "parent": parent, "offset": index,
                  "via": "direct" if index == 0 else "base"}
        remember_stat(cache_key, answer)
        return answer

    @app.get("/api/panes/{pane_id}/uploads/{name}")
    async def pane_upload_file(request: Request, pane_id: str, name: str, thumb: int = 0):
        """Serve a file that was uploaded through the composer.

        Chat bubbles only carry the file name (``user uploaded file “a.png”``), and
        the path at send time is not known any more, so look in the places an
        upload can land: ``<pane cwd>/upload_files/<name>`` first, then the cwd
        itself. Names are single segments, so ``..`` cannot escape.
        """
        require_auth(request)
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=404, detail="not found")
        try:
            pane = await asyncio.to_thread(pane_or_404, pane_id)
        except HTTPException:
            raise
        base = (pane.get("path") or "").rstrip("/")
        shots = os.path.join(base, "upload_files") if base else ""
        candidates = []
        if thumb:
            # 气泡里的预览只要一张 320px 的小图：原图动辄几 MB、解码几十 MB，
            # 十几张就能把浏览器拖垮。composer 上传时顺手存了一份 sidecar。
            stem = os.path.splitext(name)[0]
            if shots:
                candidates.append(os.path.join(shots, ".thumbs", stem + ".jpg"))
        if shots:
            candidates.append(os.path.join(shots, name))
        if base:
            candidates.append(os.path.join(base, name))
        for candidate in candidates:
            if os.path.isfile(candidate):
                media_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
                return FileResponse(candidate, media_type=media_type, headers={
                    "Cache-Control": "private, max-age=86400",
                    "X-Content-Type-Options": "nosniff",
                })
        raise HTTPException(status_code=404, detail="not found")

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
            transport, _actual, _pane = await file_target(pane_id)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        name = os.path.basename(path.rstrip("/")) or "file"
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline_types = ("text/", "image/", "video/", "audio/", "application/pdf", "application/json")
        inline = media_type.startswith(inline_types)
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
        }
        if inline:
            headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(name, safe="")
        else:
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name, safe="")
        # 和 /files/download 一样走流式：本地和远端都不再整包读进内存，慢链路
        # 上浏览器可以边下边看，而不是等整个文件读完才开始。先在线程里预取
        # 一块：文件不存在之类的错误要在返回响应前抛出来（404/503），而不是
        # 在流式回传中途变成一次无从映射的 500。
        stream_file = getattr(transport, "stream_file", None)
        if stream_file is not None:
            try:
                size = await asyncio.to_thread(transport.file_size, path)
            except (RuntimeError, OSError, ValueError):
                size = None
            if size:
                headers["Content-Length"] = str(size)
            chunks = stream_file(path, download_limit, idle_timeout=download_idle)

            def _prime():
                iterator = iter(chunks)
                try:
                    return iterator, next(iterator)
                except StopIteration:
                    return iterator, None

            try:
                stream, head = await asyncio.to_thread(_prime)
            except (TmuxError, RuntimeError, OSError, ValueError) as exc:
                close = getattr(chunks, "close", None)
                if close:
                    close()
                message = str(exc)
                if "not a file" in message or "no such file" in message.lower():
                    raise HTTPException(status_code=404, detail=message) from exc
                raise HTTPException(status_code=503, detail=message) from exc

            def relay():
                try:
                    if head is not None:
                        yield head
                    yield from stream
                finally:
                    close = getattr(chunks, "close", None)
                    if close:
                        close()

            return StreamingResponse(relay(), media_type=media_type, headers=headers)
        try:
            payload = await asyncio.to_thread(transport.download_file, path, download_limit)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            if "not a file" in message or "no such file" in message.lower():
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=503, detail=message) from exc
        headers["Content-Length"] = str(len(payload))
        return Response(content=payload, media_type=media_type, headers=headers)

    async def download_response(pane_id: str, path: str):
        """下载一个 pane 文件；普通路由和票据路由 (/t/download) 共用。"""
        if not path:
            raise HTTPException(status_code=422, detail="path is required")
        try:
            transport, _actual, _pane = await file_target(pane_id)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        name = os.path.basename(path.rstrip("/")) or "download"
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
            "Cache-Control": "no-store",
        }
        # 远端主机（尤其家用上行只有几十 KB/s）走流式：固定超时会把"慢"误判成失败
        stream_file = getattr(transport, "stream_file", None)
        if stream_file is not None:
            try:
                size = await asyncio.to_thread(transport.file_size, path)
            except (RuntimeError, OSError, ValueError):
                size = None
            if size:
                headers["Content-Length"] = str(size)
            chunks = stream_file(path, download_limit, idle_timeout=download_idle)

            def _prime():
                iterator = iter(chunks)
                try:
                    return iterator, next(iterator)
                except StopIteration:
                    return iterator, None

            try:
                stream, head = await asyncio.to_thread(_prime)
            except (TmuxError, RuntimeError, OSError, ValueError) as exc:
                close = getattr(chunks, "close", None)
                if close:
                    close()
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            def relay():
                try:
                    if head is not None:
                        yield head
                    yield from stream
                finally:
                    close = getattr(chunks, "close", None)
                    if close:
                        close()

            return StreamingResponse(relay(), media_type=media_type, headers=headers)
        try:
            payload = await asyncio.to_thread(transport.download_file, path, download_limit)
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        headers["Content-Length"] = str(len(payload))
        return Response(content=payload, media_type=media_type, headers=headers)

    @app.get("/api/panes/{pane_id}/files/download")
    async def download_pane_file(request: Request, pane_id: str, path: str):
        require_auth(request)
        if not path:
            raise HTTPException(status_code=422, detail="path is required")
        return await download_response(pane_id, path)

    @app.post("/api/panes/{pane_id}/files/zip")
    async def zip_pane_files(request: Request, pane_id: str, payload: ZipRequest):
        require_auth(request)
        wanted = [item for item in (payload.paths or []) if item.strip()]
        if not wanted:
            raise HTTPException(status_code=422, detail="select at least one file")
        if len(wanted) > 1000:
            raise HTTPException(status_code=422, detail="too many paths selected")
        try:
            transport, _actual, pane = await file_target(pane_id)
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

    async def upload_response(pane_id: str, name: str, dir: str, offset: int, total: int,
                            token: str, body: bytes):
        """写一个 pane 文件（可分块）；普通路由和票据路由 (/t/upload) 共用。"""
        """Write a file, optionally one chunk at a time.

        Chunked mode (``total`` > 0) appends into a ``.part`` file and renames it
        when the last byte lands, so a dropped connection loses one chunk instead
        of the whole upload - and no request is ever large enough for Cloudflare's
        100 MB body limit.  ``offset`` may come back as 409 with the size the far
        side actually has, which is how a resumed upload finds its place.
        """
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise HTTPException(status_code=422, detail="invalid file name")
        if token and (len(token) > 64 or not all(ch.isalnum() or ch in "-_" for ch in token)):
            raise HTTPException(status_code=422, detail="invalid upload token")
        try:
            transport, _actual, pane = await file_target(pane_id)
            directory = dir or pane.get("path") or "."
            if len(body) > upload_limit:
                raise HTTPException(status_code=413,
                                    detail=f"file exceeds the upload limit of {upload_limit} bytes")
            if total == 0 and len(body) > single_shot_upload_max:
                raise HTTPException(status_code=413,
                                    detail="use chunked upload (total/offset/token) for files over 4 MB")
            target = os.path.join(directory, name)
            if total > 0:
                if total > upload_limit:
                    raise HTTPException(status_code=413,
                                        detail=f"file exceeds the upload limit of {upload_limit} bytes")
                part = os.path.join(directory, f".{token or 'upload'}-{name}.part")
                try:
                    written = await asyncio.to_thread(transport.upload_chunk, part, body, offset)
                except UploadOffsetMismatch as exc:
                    # 头部 + body 都给：客户端从哪边读都行
                    return JSONResponse(status_code=409,
                                        content={"detail": "upload offset mismatch", "offset": exc.offset},
                                        headers={"X-Upload-Offset": str(exc.offset)})
                if written >= total:
                    await asyncio.to_thread(transport.finish_upload, part, target)
                    return {"status": "uploaded", "path": target, "size": written}
                return {"status": "partial", "path": target, "offset": written, "size": total}
            await asyncio.to_thread(transport.upload_file, target, body)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "uploaded", "path": target, "size": len(body)}

    @app.put("/api/panes/{pane_id}/files/upload")
    async def upload_pane_file(request: Request, pane_id: str, name: str, dir: str = "",
                               offset: int = 0, total: int = 0, token: str = ""):
        require_auth(request)
        return await upload_response(pane_id, name, dir, offset, total, token, await request.body())

    # ---- 直连传输（票据代替 cookie）-----------------------------------------
    #
    # 用户永远只记得住公网那一个域名，所以页面照旧从 Cloudflare 加载；只有文件
    # 字节在"用户在局域网/tailnet 里"时改走直连源站。直连源站是另一个 host，
    # 读不到会话 cookie，于是用一枚短命票据作为凭据 —— 票据绑定 pane/路径/方向/
    # 大小上限，10 分钟过期，用量也记账。

    def ticket_or_403(token: str, direction: str) -> dict:
        try:
            payload = transfer_tickets.peek(transfer_secret, token)
            if payload.get("d") != direction:
                raise transfer_tickets.TicketError("ticket is for another direction")
            transfer_ledger.spent(payload)
        except transfer_tickets.TicketError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return payload

    @app.get("/t/ping")
    async def transfer_ping():
        """探测用：只回答"这里是不是 TmuxWeb"，不带任何用户信息、不需要鉴权。"""
        return Response(content='{"tmuxweb":true}', media_type="application/json",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/transfer/ticket")
    async def mint_transfer_ticket(request: Request, payload: TransferTicketRequest):
        """给直连源站签一张传输票据；只有已登录的控制台会话能签。"""
        require_auth(request)
        direction = (payload.direction or "down").strip().lower()
        if direction not in {"down", "up"}:
            raise HTTPException(status_code=422, detail="direction must be up or down")
        if not payload.pane or not payload.path:
            raise HTTPException(status_code=422, detail="pane and path are required")
        if direction == "up" and payload.size <= 0:
            raise HTTPException(status_code=422, detail="size is required for an upload")
        if payload.size > upload_limit:
            raise HTTPException(status_code=413,
                                detail=f"file exceeds the upload limit of {upload_limit} bytes")
        ticket = transfer_tickets.mint(
            transfer_secret, pane=payload.pane, path=payload.path, direction=direction,
            max_bytes=int(payload.size), name=payload.name,
            # 上传不限次数：块大小是自适应的，事先估出来的块数一定不准
            uses=-1 if direction == "up" else 0,
        )
        return ticket

    @app.get("/t/download")
    async def transfer_download(t: str = ""):
        payload = ticket_or_403(t, "down")
        return await download_response(payload["p"], payload["f"])

    @app.put("/t/upload")
    async def transfer_upload(request: Request, t: str = "", name: str = "", dir: str = "",
                              offset: int = 0, total: int = 0, token: str = ""):
        payload = ticket_or_403(t, "up")
        if name and name != payload.get("n"):
            raise HTTPException(status_code=403, detail="ticket is for another file")
        if total and payload.get("m") and int(total) > int(payload["m"]):
            raise HTTPException(status_code=403, detail="ticket is for a smaller file")
        return await upload_response(payload["p"], payload.get("n") or name, dir, offset, total,
                                     token, await request.body())

    @app.delete("/api/panes/{pane_id}/files/upload")
    async def delete_pane_upload(request: Request, pane_id: str, name: str, dir: str = ""):
        """删掉聊天里刚上传的附件（只允许 upload_files 目录下的单层文件名）。"""
        require_auth(request)
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise HTTPException(status_code=422, detail="invalid file name")
        if os.path.basename(dir.rstrip("/")) != "upload_files":
            raise HTTPException(status_code=422, detail="only files in upload_files can be removed here")
        try:
            transport, _actual, _pane = await file_target(pane_id)
            target = os.path.join(dir, name)
            await asyncio.to_thread(transport.delete_file, target)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "deleted", "path": target}

    @app.post("/api/panes/{pane_id}/files/rename")
    async def rename_pane_file(request: Request, pane_id: str, payload: FileRenameRequest):
        """Rename one entry inside its own directory (no moves, never overwrites)."""
        require_auth(request)
        try:
            source = guard_panel_path(payload.path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        name = payload.name.strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise HTTPException(status_code=422, detail="invalid file name")
        try:
            transport, _actual, _pane = await file_target(pane_id)
            target = await asyncio.to_thread(transport.rename_path, source, name)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "renamed", "path": target, "name": name}

    @app.post("/api/panes/{pane_id}/files/delete")
    async def delete_pane_files(request: Request, pane_id: str, payload: FileDeleteRequest):
        """Delete selected entries; a non-empty directory needs ``recursive``."""
        require_auth(request)
        wanted: list[str] = []
        for raw in payload.paths:
            try:
                wanted.append(guard_panel_path(raw))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            transport, _actual, _pane = await file_target(pane_id)
        except HTTPException:
            raise
        except (TmuxError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        deleted: list[str] = []
        failed: list[dict] = []
        for target in wanted:
            try:
                await asyncio.to_thread(transport.delete_path, target, payload.recursive)
                deleted.append(target)
            except (TmuxError, RuntimeError, OSError, ValueError) as exc:
                failed.append({"path": target, "error": str(exc)})
        if not deleted and failed:
            raise HTTPException(status_code=409, detail=failed[0]["error"])
        return {"status": "deleted" if not failed else "partial",
                "deleted": deleted, "failed": failed}

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
        provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
        host_name = host_label(pane_id)
        entry.update({"provider": provider, "pane_title": pane.get("title") or "",
                      "path": pane.get("path") or "", "session": pane.get("session") or "",
                      "window": pane.get("target") or "", "command": pane.get("command") or "",
                      "host": host_name})
        entry["default_title"] = default_card_title(pane, host_name) or (pane.get("title") or pane_id)
        if not custom_title:
            entry["title"] = entry["default_title"]
        try:
            entry["status"] = relay.inspect(transport, actual_pane_id, provider, lines=30, pane=pane)["status"]
        except (RuntimeError, OSError, ValueError):
            entry["status"] = "unknown"
        if not provider:
            return entry
        try:
            data = transport.transcript(actual_pane_id, 4, pane=pane)
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
            entry["waiting"] = bool(with_waiting_hint(
                data, pane if "~" not in pane_id else None).get("waiting"))
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

    @app.get("/api/home/recent")
    async def home_recent(request: Request, limit: int = 8):
        """Recent pane visits plus the pinned ones, for the sidebar's 最近 block.

        Pinned panes come first (newest pin first) and are left out of the recent
        tail so a row never shows up twice; ``favorite`` lets the row render its
        star without a second request.
        """
        require_auth(request)
        rows = await asyncio.to_thread(home.recent, max(1, min(int(limit), 20)))
        pins = await asyncio.to_thread(home.pins)
        favorite_rows = await asyncio.to_thread(home.favorites)
        favorites = {row["pane_id"] for row in favorite_rows}
        titles = {row["pane_id"]: (row.get("title") or "") for row in favorite_rows}
        seen: set[str] = set()
        entries: list[dict] = []
        visits = {row["pane_id"]: row["visited_at"] for row in rows}
        for pin in pins:
            pane = pin["pane_id"]
            seen.add(pane)
            entries.append({
                "pane": pane,
                "label": pin.get("label", ""),
                "pinned": True,
                "favorite": pane in favorites,
                "title": titles.get(pane, ""),
                "visited_at": visits.get(pane, 0),
            })
        for row in rows:
            if row["pane_id"] in seen:
                continue
            entries.append({
                "pane": row["pane_id"],
                "label": "",
                "pinned": False,
                "favorite": row["pane_id"] in favorites,
                "title": titles.get(row["pane_id"], ""),
                "visited_at": row["visited_at"],
            })
        # 侧栏的每一行都带 #N：和会话树、MCP relay 用的是同一套编号
        numbers = await asyncio.to_thread(home.ensure_numbers, [entry["pane"] for entry in entries])
        for entry in entries:
            entry["number"] = numbers.get(entry["pane"])
        return {"recent": entries, "pinned": [pin["pane_id"] for pin in pins]}

    @app.post("/api/home/recent/pin")
    async def pin_home_recent(request: Request, payload: HomePinRequest):
        """Nail a pane to the top of 最近 (or let it go again)."""
        require_auth(request)
        try:
            split_pane_ref(payload.pane)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid pane reference") from exc
        if payload.pinned:
            row = await asyncio.to_thread(home.pin, payload.pane, payload.label)
            return {"pane": row["pane_id"], "pinned": True, "label": row["label"]}
        removed = await asyncio.to_thread(home.unpin, payload.pane)
        return {"pane": payload.pane, "pinned": False, "removed": removed}

    @app.patch("/api/home/recent/pin/{pane_id}")
    async def rename_home_pin(request: Request, pane_id: str, payload: HomePinRenameRequest):
        """Give a pinned pane its own name (empty restores the generated one).

        A pane has one name: when it is also on the board the card title is updated
        in the same step, so renaming from the sidebar or from the board stays in step.
        """
        require_auth(request)
        try:
            row = await asyncio.to_thread(home.rename_pin, pane_id, payload.label)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="pane is not pinned") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        forget_home_entry(pane_id)
        return {"pane": row["pane_id"], "label": row["label"], "pinned": True}

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
        # Never wait out a whole sweep here: answer from the cache at once when
        # it holds anything, and only a cold cache waits, briefly.
        states = await asyncio.to_thread(pane_states_cached, 0.5, 30.0, True)
        return {"panes": [{key: item.get(key) for key in ("id", "provider", "status", "status_source")}
                          for item in states]}

    async def perform_relay_ask(payload: RelayAskRequest) -> dict:
        """Ask a pane and wait for the answer: used by the HTTP route and the MCP endpoint."""
        transport, pane_id, pane, host_name = await asyncio.to_thread(relay_target, payload.target)
        provider = pane.get("provider") or transcript_reader.provider_for_command(pane.get("command"))
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

    @app.websocket("/ws/panes/{pane_id}")
    async def stream_pane(websocket: WebSocket, pane_id: str):
        token = websocket.cookies.get(COOKIE_NAME, "")
        if not hmac.compare_digest(token, expected_token()):
            await websocket.close(code=4401)
            return
        try:
            pane = await asyncio.to_thread(pane_or_404, pane_id)
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
        misses = 0
        try:
            while True:
                snapshot = getattr(transport, "snapshot_ansi", None)
                try:
                    if snapshot is None:
                        capture_ansi = getattr(transport, "capture_ansi", transport.capture)
                        content = await asyncio.to_thread(capture_ansi, actual_pane_id, lines, pane=pane)
                        new_digest = hashlib.sha1(content.encode()).hexdigest()
                        payload = None if digest == new_digest else content
                        digest = new_digest
                    else:
                        digest, payload = await asyncio.to_thread(snapshot, actual_pane_id, lines, digest, pane=pane)
                    misses = 0
                except (RuntimeError, OSError, ValueError):
                    # 慢链路（或正在上传/下载大文件）会让一次抓屏超时；那是"这次没抓到"，
                    # 不是"这个 pane 没了"。以前这里直接 return，浏览器就断了。
                    misses += 1
                    if misses >= 8:
                        raise
                    await asyncio.sleep(min(5.0, 0.8 * misses))
                    continue
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
            await asyncio.to_thread(pane_or_404, pane_id)
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

    # ---------------------------------------------------------------- plugins
    # A feature can live in plugins/<name>/ and be left out entirely: the relay is the first one. The
    # core hands plugins a small, documented set of capabilities (PluginServices) and nothing else.
    plugin_services = PluginServices(
        require_auth=require_auth,
        plugins_root=str(BASE_DIR / "plugins"),
        data_root=str(BASE_DIR),
        tmux=tmux,
        tmux_error=TmuxError,
        remote_hosts=remote_hosts,
        home_store=home,
        share_store=None,          # shares are still core-only; expose one when a plugin needs it
        relay_log=relay_log,
        relay_ask=perform_relay_ask,
        relay_target=relay_target,
        collect_pane_states=collect_pane_states,
        resend=resend_manager,
        version=os.environ.get("TMUX_WEB_VERSION", ""),
    )
    # Same rule as the password file: an app built with an explicit password (the tests, a throwaway
    # instance) must not adopt the deployment's plugin decisions.
    plugin_state_path = (
        plugin_state
        if plugin_state is not None
        else os.environ.get("TMUX_WEB_PLUGIN_STATE")
        or (None if password is not None else str(BASE_DIR / "plugins.json"))
    )
    manager = PluginManager(app, plugin_services, directory=plugins_dir, enabled=plugins,
                            state_path=plugin_state_path, logger=logging.getLogger("tmuxweb.plugins"))
    plugin_manager["mgr"] = manager
    manager.load_all()
    # A plugin may offer its own runner factory (the agent plugin does):
    # it becomes make_remote_runner unless the caller passed an explicit
    # remote_runner_factory — the tests do, so their fake is untouched.
    # runner_for is a closure that reads make_remote_runner at call time,
    # so reassigning it here, after load_all, is effective.
    plugin_runner_factory = manager.runner_factory()
    make_remote_runner = remote_runner_factory or plugin_runner_factory or SSHRunner
    agent_status_provider = manager.host_status
    install_routes(app, manager, require_auth)
    app.state.plugins = manager
    app.router.add_event_handler("shutdown", manager.shutdown)

    return app


if __name__ == "__main__":
    uvicorn.run(
        "tmux_web:create_app",
        host=os.environ.get("TMUX_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TMUX_WEB_PORT", "8787")),
        factory=True,
        reload=False,
        # 经过 Cloudflare：keep-alive 75s（在 CF 100s 空闲超时之内，不断建连），
        # 优雅停机 30s（.env 改动重启时让在途上传/WS 排空，而不是直接掐断变 502），
        # WS 协议层心跳 20s（空闲连接不会被中间代理静默掐掉）。
        timeout_keep_alive=75,
        timeout_graceful_shutdown=10,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
