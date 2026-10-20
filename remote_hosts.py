from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
import time
import tempfile
import shlex
import fcntl
import struct
import termios
import asyncio
from pathlib import Path

from cryptography.fernet import Fernet

HOST_ID_RE = re.compile(r"^[a-f0-9]{16}$")
PANE_ID_RE = re.compile(r"^%[0-9]+$")
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


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


def parse_tmux_panes(raw: str, host_id: str) -> list[dict]:
    panes = []
    for line in raw.splitlines():
        fields = line.split("\t")
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
        if auth_type not in {"password", "key"}:
            raise ValueError("invalid auth type")
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
                identity_file: str | None = None) -> tuple[list[str], dict[str, str]]:
        command = [
            "ssh", "-T" if not tty else "-tt", "-p", str(self.host["port"]),
            "-o", "BatchMode=no", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "LogLevel=ERROR",
        ]
        if identity_file:
            command += ["-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
                        "-o", "IdentitiesOnly=yes", "-i", identity_file]
        elif self.host.get("auth_type") == "password":
            command += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                        "-o", "KbdInteractiveAuthentication=no", "-o", "NumberOfPasswordPrompts=1"]
        command += [f"{self.host['username']}@{self.host['hostname']}", remote_command]
        environment = {"SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"}
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
            else:
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
                return subprocess.run(command, input=input_bytes, capture_output=True, timeout=timeout,
                                      check=False, env=environment, pass_fds=pass_fds, start_new_session=True)
            finally:
                if read_fd is not None:
                    os.close(read_fd)

    def check(self) -> str:
        completed = self.run("printf TMUX_REMOTE_OK", timeout=12)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.decode(errors="replace").strip() or "SSH connection failed")
        return completed.stdout.decode(errors="replace")

    def list_panes(self) -> list[dict]:
        fmt = "\t".join([
            "#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}", "#{session_name}",
            "#{window_index}", "#{window_name}", "#{pane_index}", "#{pane_current_command}",
            "#{pane_title}", "#{pane_current_path}", "#{pane_active}", "#{pane_width}", "#{pane_height}",
        ])
        command = f"tmux list-panes -a -F {shlex.quote(fmt)}"
        completed = self.run(command)
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
        else:
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
