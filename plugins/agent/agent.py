"""TmuxWeb agent transport: the counterparty program that runs on a remote host.

The remote host today is only reachable over SSH. This program is the
optional alternative transport: it runs on the remote host, dials back to
the TmuxWeb host over a persistent WebSocket, and executes the shell
commands the server sends it, so one connection replaces one SSH round
trip per operation.

Protocol (shared with the server-side plugin; do not change unilaterally):

* JSON over WebSocket text frames.
* After connecting, the agent sends ``hello``::

      {"type": "hello", "token": "<token>",
       "agent": {"version": "1.0.0", "hostname": "<hostname>",
                 "tmux": "<tmux path or empty string>"}}

  The server replies ``{"type": "welcome", "host_id": ...,
  "host_name": ...}``; a wrong token gets the connection closed with
  code 4403.
* Server requests are always batch-shaped::

      {"id": "<uuid>", "ops": [{"op": "exec", "command": "<shell>",
                                "input_b64": "<base64 or empty>",
                                "timeout": 15}]}

  v1 sends one op per batch, but a batch may hold several ops and they
  run in order. Reply::

      {"id": "...", "ok": true,
       "results": [{"ok": true, "stdout_b64": "...", "stderr_b64": "...",
                    "returncode": 0}]}

  A single failing op is ``{"ok": false, "error": "..."}`` inside
  ``results``; a malformed whole batch is ``{"id": "...", "ok": false,
  "error": "..."}``.
* The agent sends ``{"type": "heartbeat", "at": <unix timestamp>}``
  every 30 seconds.
* v1 op allowlist is just ``"exec"``. An unknown op yields an error
  result; it never breaks the loop.

Standard library only (Python 3.9+). No third-party imports.

Usage::

    agent.py --server ws://host:8787/agent --token <token> [--tmux /usr/bin/tmux]
    agent.py --config ~/.config/tmuxweb-agent/config.json
    agent.py install --server ... --token ... [--token-file PATH] [--tmux ...]
    agent.py status
    agent.py uninstall

The token is never written to stdout, stderr logs, or any file other
than the 0600 config file ``install`` writes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import queue
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

VERSION = "1.0.0"
AGENT_NAME = "tmuxweb-agent"
HEARTBEAT_INTERVAL = 30.0
DEFAULT_EXEC_TIMEOUT = 15.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024  # per stream (stdout / stderr)
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # one incoming WebSocket message
BACKOFF_CAP = 60.0
# How many ops of one batch run at once. The batch still returns a
# single reply, in op order; the pool only overlaps the waiting.
_BATCH_MAX_WORKERS = 8
# One fragment of a large outbound message. A fragment is one sendall,
# so a pong (or a close) queued while a big reply is in flight waits at
# most one fragment: at the ~50KB/s a relayed tailnet delivers, 16KB
# costs ~0.3s — far inside the server's 20s ping timeout, where a
# single 3MB sendall cost the whole transfer and starved pings to death.
_SEND_FRAGMENT_BYTES = 16384
# Cap on the socket's send buffer (SO_SNDBUF). Fragmentation alone does
# not save the pong: the sender can copy fragments into the kernel
# faster than a slow link drains them, so a pong written between two
# fragments still queues behind hundreds of KB of reply bytes in the
# kernel and arrives minutes too late — that is exactly what the
# slow-link test caught (pong 9.5s late with fragmentation only).
# Capping the buffer makes the sender pace itself to the drain rate,
# so a pong never has more than one buffer's worth of reply bytes
# ahead of it. Linux doubles the value (64KB effective); macOS uses it
# as-is. Even the smaller end still allows ~160KB/s at a 200ms RTT,
# three times the ~50KB/s the relayed tailnet actually delivers, so
# this costs no throughput where the agent runs — and on a fast link
# the buffer simply stays full and drains at line rate.
_SEND_BUFFER_BYTES = 32768
# Per-fragment socket timeout for the sender thread. A slow link must
# never kill a reply the way the reader's old 5s recv timeout did: a
# fragment that makes no progress for 30s means the peer is gone, not
# merely slow. The reader never touches this timeout (see _read_frame).
_SEND_TIMEOUT = 30.0
# How many messages run at once (see _MessagePool). The pool exists so
# one slow message cannot block the others, not to parallelise CPU
# work: the tmux server itself serialises commands, so more workers
# would only add threads, never speed. Eight covers the shapes the
# server actually sends at once (a sweep batch, transcript polls, a
# capture, a send). Worst-case threads: 8 message workers, each with
# its own op pool of up to 8 threads plus 3 helper threads per exec
# (two drains, one stdin writer) — about 260 transient threads if 8
# full 8-op batches ever overlapped; in practice the server sends at
# most ~5 concurrent messages per host with batches of at most 2 ops,
# so the steady state stays under 30.
_MESSAGE_MAX_WORKERS = 8
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONNECT_TIMEOUT = 10.0

# WebSocket opcodes (RFC 6455).
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# Close code the server uses to reject a bad token.
CLOSE_AUTH_FAILED = 4403


def log(message: str) -> None:
    """One timestamped line to stderr. Never call this with a token."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"{stamp} [{AGENT_NAME}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455, no third-party code)
# ---------------------------------------------------------------------------

class WebSocketError(Exception):
    """Handshake or framing failure."""


class ConnectionClosed(Exception):
    """The peer closed the connection (or the socket died)."""

    def __init__(self, code: int = 1006, reason: str = "") -> None:
        super().__init__(f"connection closed ({code}): {reason or 'no reason'}")
        self.code = code
        self.reason = reason


def encode_frame(payload: bytes, *, opcode: int = OP_TEXT, fin: bool = True,
                 mask: bool = True, mask_key: bytes | None = None) -> bytes:
    """Encode one WebSocket frame. Client frames are masked (RFC 6455)."""
    if len(payload) >= 1 << 63:
        raise ValueError("payload too large for a WebSocket frame")
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if mask:
        key = mask_key if mask_key is not None else os.urandom(4)
        if len(key) != 4:
            raise ValueError("mask key must be 4 bytes")
        header.extend(key)
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + payload


def parse_one_frame(data: bytes, offset: int = 0):
    """Parse one frame at ``offset``.

    Returns ``(fin, opcode, payload, next_offset)``, or ``None`` when
    ``data`` does not yet hold a complete frame. Raises ValueError on a
    structurally invalid frame.
    """
    if len(data) - offset < 2:
        return None
    b1, b2 = data[offset], data[offset + 1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    pos = offset + 2
    if length == 126:
        if len(data) - pos < 2:
            return None
        (length,) = struct.unpack_from("!H", data, pos)
        pos += 2
    elif length == 127:
        if len(data) - pos < 8:
            return None
        (length,) = struct.unpack_from("!Q", data, pos)
        pos += 8
        if length >= 1 << 63:
            raise ValueError("frame length overflows 63 bits")
    if masked:
        if len(data) - pos < 4:
            return None
        mask_key = data[pos:pos + 4]
        pos += 4
    if len(data) - pos < length:
        return None
    payload = bytes(data[pos:pos + length])
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, pos + length


def decode_frames(data: bytes):
    """Decode every complete frame in ``data``; ValueError if truncated."""
    frames = []
    offset = 0
    while offset < len(data):
        parsed = parse_one_frame(data, offset)
        if parsed is None:
            raise ValueError("truncated frame data")
        fin, opcode, payload, offset = parsed
        frames.append((fin, opcode, payload))
    return frames


def _expected_accept(key: str) -> str:
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class _SendItem:
    """One queued outbound frame for the sender thread.

    ``kind`` is ``"text"`` (a reply, heartbeat or hello — fragmented
    when large) or ``"control"`` (a pong or a close frame, always one
    frame). ``done`` fires when the item was written (or failed), and
    ``error`` carries the failure for the waiting sender.
    """

    __slots__ = ("kind", "opcode", "payload", "fin", "done", "error")

    def __init__(self, kind, opcode, payload, fin=True):
        self.kind = kind
        self.opcode = opcode
        self.payload = payload
        self.fin = fin
        self.done = threading.Event()
        self.error = None


class WSConnection:
    """A blocking WebSocket connection with a buffered frame reader.

    One instance is used from several threads: a reader thread calls
    :meth:`read_message`, message workers and the main loop call
    :meth:`send_text`, and a dedicated sender thread owns every byte
    written to the socket. That split is the whole point: on a slow
    link a big reply used to be one giant ``sendall`` holding the send
    lock, so the reader could not answer a ping for the whole transfer
    — the server's ping timeout fired, the reply was abandoned
    mid-frame (leaving a partial frame that queued every later pong
    behind it in the TCP send queue), and the connection flapped. Now
    a large message goes out as 64KB fragments and a pong or close
    queued mid-transfer is written between two fragments, so the
    connection survives a transfer of any length; and the socket
    timeout the sender uses is a generous per-fragment budget, never
    the reader's short recv timeout. The send buffer is capped as
    well (see _SEND_BUFFER_BYTES): without that, fragments copied
    into the kernel faster than a slow link drains them would still
    queue a pong behind the whole reply.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()
        # Bound the kernel send queue (see _SEND_BUFFER_BYTES): without
        # this the sender copies fragments into the kernel faster than
        # a slow link drains them, and a pong written between two
        # fragments still queues behind the whole reply. Setting
        # SO_SNDBUF also locks the size (no autotuning), so the bound
        # holds for the life of the connection. Best effort: if the
        # platform refuses it, fragmentation alone still bounds the
        # sender's busy time per fragment.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                            _SEND_BUFFER_BYTES)
        except OSError as exc:
            log(f"could not cap the send buffer: {exc}")
        # Control first, replies second: the sender always drains the
        # control queue between two fragments of a big reply.
        self._control_queue: queue.Queue = queue.Queue()
        self._data_queue: queue.Queue = queue.Queue()
        # Guards "is the sender still alive?" + enqueue, so a send
        # can neither slip in after the sender's final drain (and wait
        # forever) nor race it.
        self._enqueue_lock = threading.Lock()
        self._send_error = None
        self._sender_done = threading.Event()
        self._close_sent = False
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="tmuxweb-agent-send", daemon=True)
        self._sender_thread.start()

    # -- sender thread --------------------------------------------------
    def _sender_loop(self) -> None:
        """Write queued frames until a close is asked or a write fails.

        Runs on its own thread and is the only code that ever writes
        to the socket, so frames can never interleave and the reader
        never blocks on a send. A write failure is fatal to the
        connection: the socket is shut down (the reader learns it from
        its next read and the daemon reconnects) and every queued item
        fails, so no send_text waits forever.
        """
        try:
            self._sock.settimeout(_SEND_TIMEOUT)
            while True:
                item = self._take_item()
                if item.kind == "control":
                    if item.opcode == OP_CLOSE:
                        self._write_frame(OP_CLOSE, item.payload)
                        item.done.set()
                        self._send_error = ConnectionClosed(
                            1000, "connection closed by request")
                        return
                    self._write_frame(item.opcode, item.payload)
                    item.done.set()
                    continue
                try:
                    self._send_fragmented(item)
                except Exception as exc:  # noqa: BLE001 - item carries it
                    item.error = exc
                    item.done.set()
                    raise
                item.done.set()
        except Exception as exc:  # noqa: BLE001 - sender never dies quietly
            if self._send_error is None:
                self._send_error = exc
        finally:
            self._sender_done.set()
            self._fail_queued()
            self._shutdown_socket()

    def _take_item(self) -> _SendItem:
        """Next item to send: a queued control frame beats a reply."""
        while True:
            try:
                return self._control_queue.get(timeout=0.1)
            except queue.Empty:
                pass
            try:
                return self._data_queue.get_nowait()
            except queue.Empty:
                continue

    def _drain_control(self) -> bool:
        """Send every queued control frame now.

        Runs between two fragments of a big reply, which is how a pong
        gets out while the reply is still in flight. Returns True when
        a close was asked: the current message is abandoned and the
        sender exits.
        """
        while True:
            try:
                item = self._control_queue.get_nowait()
            except queue.Empty:
                return False
            if item.opcode == OP_CLOSE:
                self._write_frame(OP_CLOSE, item.payload)
                item.done.set()
                self._send_error = ConnectionClosed(
                    1000, "connection closed by request")
                return True
            self._write_frame(item.opcode, item.payload)
            item.done.set()

    def _send_fragmented(self, item: _SendItem) -> None:
        """Write a text message as 16KB fragments (RFC 6455 section 5.4).

        Control frames may legally sit between two fragments, so a
        pong queued mid-message is written before the next fragment
        instead of behind the whole message — and the capped send
        buffer (see __init__) means it also has at most one buffer's
        worth of reply bytes ahead of it in the kernel. A fragment
        that makes no progress for _SEND_TIMEOUT raises and kills the
        sender (see _sender_loop): a half-sent fragment on a dead
        connection is better than a hung one, because the daemon
        reconnects and the server fails the op at once instead of
        timing it out.
        """
        payload = item.payload
        offset = 0
        first = True
        while True:
            chunk = payload[offset:offset + _SEND_FRAGMENT_BYTES]
            fin = offset + len(chunk) >= len(payload)
            self._write_frame(
                OP_TEXT if first else OP_CONTINUATION, chunk, fin=fin)
            offset += len(chunk)
            first = False
            if fin:
                return
            if self._drain_control():
                raise ConnectionClosed(1000, "close requested mid-message")

    def _write_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        """One frame onto the socket. Sender thread only: no lock."""
        self._sock.sendall(encode_frame(payload, opcode=opcode, fin=fin, mask=True))

    def _fail_queued(self) -> None:
        """Fail every item still queued, so no send_text waits forever."""
        error = self._send_error
        if error is None:
            error = ConnectionClosed(1006, "sender stopped")
        with self._enqueue_lock:
            for source in (self._control_queue, self._data_queue):
                while True:
                    try:
                        item = source.get_nowait()
                    except queue.Empty:
                        break
                    item.error = error
                    item.done.set()

    def _shutdown_socket(self) -> None:
        """Shutdown + close the socket, ignoring errors. Idempotent."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_error_text(self) -> str:
        if self._send_error is None:
            return "sender stopped"
        return str(self._send_error)

    # -- handshake ------------------------------------------------------
    @classmethod
    def connect(cls, url: str, timeout: float = CONNECT_TIMEOUT) -> "WSConnection":
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme in server URL: {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise WebSocketError(f"no host in server URL: {url!r}")
        secure = parsed.scheme == "wss"
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WebSocketError(f"cannot connect to {host}:{port}: {exc}") from exc
        if secure:
            try:
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            except (ssl.SSLError, OSError) as exc:
                sock.close()
                raise WebSocketError(f"TLS handshake failed: {exc}") from exc
        sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if port in (80, 443) else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        try:
            sock.sendall(request.encode("ascii"))
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("server closed the connection during handshake")
                raw += chunk
                if len(raw) > 65536:
                    raise WebSocketError("handshake response too large")
        except OSError as exc:
            sock.close()
            raise WebSocketError(f"handshake failed: {exc}") from exc

        head, _, _ = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError) as exc:
            sock.close()
            raise WebSocketError(f"bad handshake status line: {lines[0]!r}") from exc
        if status != 101:
            sock.close()
            raise WebSocketError(f"handshake rejected with HTTP {status}")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                # Names are case-insensitive; values are NOT — the
                # Sec-WebSocket-Accept is base64, and lowercasing it
                # breaks the comparison (RFC 6455 section 4.1).
                headers[name.strip().lower()] = value.strip()
        if "websocket" not in headers.get("upgrade", "").lower():
            sock.close()
            raise WebSocketError("handshake response is missing 'Upgrade: websocket'")
        if "upgrade" not in headers.get("connection", "").lower():
            sock.close()
            raise WebSocketError("handshake response is missing 'Connection: Upgrade'")
        if headers.get("sec-websocket-accept") != _expected_accept(key):
            sock.close()
            raise WebSocketError("handshake Sec-WebSocket-Accept mismatch")
        return cls(sock)

    # -- framing --------------------------------------------------------
    def _wait_readable(self) -> bool:
        """True when a recv would return promptly (or the socket died).

        An SSLSocket can hold decrypted bytes ``select`` cannot see,
        so ask it first; a plain socket goes straight to ``select``.
        A closed socket raises OSError/ValueError, which the caller
        turns into ConnectionClosed.
        """
        pending = getattr(self._sock, "pending", None)
        if callable(pending) and pending():
            return True
        readable, _, _ = select.select([self._sock], [], [], 5.0)
        return bool(readable)

    def _read_frame(self):
        """Read one raw frame, reassembling nothing; blocks until it arrives.

        Waits with ``select`` instead of a socket timeout: the sender
        thread owns the socket timeout (a generous per-fragment send
        budget), and this thread must never shrink it to a short recv
        budget — that leak is what used to kill a big reply mid-send.
        """
        while True:
            parsed = parse_one_frame(bytes(self._buf))
            if parsed is not None:
                fin, opcode, payload, consumed = parsed
                del self._buf[:consumed]
                if len(payload) > MAX_MESSAGE_BYTES:
                    raise WebSocketError("incoming frame exceeds the size limit")
                return fin, opcode, payload
            try:
                if not self._wait_readable():
                    continue
            except (OSError, ValueError) as exc:
                raise ConnectionClosed(1006, str(exc)) from exc
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                raise ConnectionClosed(1006, str(exc)) from exc
            if not chunk:
                raise ConnectionClosed(1006, "connection closed by peer")
            self._buf += chunk
            if len(self._buf) > MAX_MESSAGE_BYTES + 1024:
                raise WebSocketError("incoming data exceeds the size limit")

    def read_message(self) -> str:
        """Read one complete text message, reassembling fragments.

        Ping frames are answered with pong inline; pong frames are
        ignored; a close frame is echoed and raises ConnectionClosed.
        The pong is only *queued* here — the sender thread writes it,
        so answering a ping never blocks on a reply in flight.
        """
        fragments: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code, reason = 1005, ""
                if len(payload) >= 2:
                    (code,) = struct.unpack_from("!H", payload, 0)
                    reason = payload[2:].decode("utf-8", "replace")
                if not self._close_sent:
                    try:
                        self.send_frame(OP_CLOSE, payload)
                    except Exception:
                        pass
                    self._close_sent = True
                raise ConnectionClosed(code, reason)
            if opcode in (OP_TEXT, OP_BINARY):
                if fragments:
                    raise WebSocketError("new data frame before previous message finished")
                fragments.append(payload)
                if fin:
                    break
            elif opcode == OP_CONTINUATION:
                if not fragments:
                    raise WebSocketError("continuation frame with no message in progress")
                fragments.append(payload)
                if fin:
                    break
            else:
                raise WebSocketError(f"unsupported opcode {opcode}")
        data = b"".join(fragments)
        if len(data) > MAX_MESSAGE_BYTES:
            raise WebSocketError("incoming message exceeds the size limit")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebSocketError(f"incoming message is not valid utf-8: {exc}") from exc

    def send_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        """Queue a control frame (pong / close echo). Never blocks.

        The sender thread writes it — between two fragments of a big
        reply when one is in flight — so answering a ping can never
        stall behind a reply. A frame queued after the sender died is
        dropped: the reader learns the connection is gone from its
        next read, and a pong for a dead connection means nothing.
        """
        item = _SendItem("control", opcode, payload, fin)
        with self._enqueue_lock:
            if self._sender_done.is_set():
                return
            self._control_queue.put(item)

    def send_text(self, text: str) -> None:
        """Queue a text message and wait until the sender wrote it all.

        The wait is per-message: the sender thread sends other
        messages and queued pongs meanwhile, so one slow reply never
        holds up the connection. Raises ConnectionClosed when the
        sender thread is gone or the write failed — the caller must
        answer with an error result instead of dropping the reply
        (see _send_reply).
        """
        item = _SendItem("text", OP_TEXT, text.encode("utf-8"))
        with self._enqueue_lock:
            if self._sender_done.is_set():
                raise ConnectionClosed(1006, self._send_error_text())
            self._data_queue.put(item)
        item.done.wait()
        if item.error is not None:
            raise ConnectionClosed(1006, str(item.error))

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Best-effort close handshake, then close the socket.

        The close frame is queued for the sender thread, so it goes
        out between two fragments of a big reply like a pong does;
        when the sender is already gone the socket is closed directly.
        Never blocks: a close must not wait behind a reply.
        """
        if self._close_sent:
            self._shutdown_socket()
            return
        self._close_sent = True
        payload = struct.pack("!H", code) + reason.encode("utf-8")
        item = _SendItem("control", OP_CLOSE, payload)
        with self._enqueue_lock:
            if self._sender_done.is_set():
                self._shutdown_socket()
                return
            self._control_queue.put(item)


def _close_quietly(conn: WSConnection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# tmux discovery
# ---------------------------------------------------------------------------

# Where to look for tmux, in the same order as the SSH probe
# (remote_hosts.TMUX_PROBE_SCRIPT): PATH first, then the well-known
# locations a non-interactive shell does not have on PATH (macOS keeps
# tmux in /opt/homebrew/bin), then a login shell as a last resort.
TMUX_CANDIDATE_PATHS = (
    "/opt/homebrew/bin/tmux",
    "/usr/local/bin/tmux",
    "/opt/local/bin/tmux",
    "~/bin/tmux",
    "~/.local/bin/tmux",
)

_LOGIN_SHELLS = ("bash", "zsh")
_PROBE_TIMEOUT = 5.0


def _command_v_tmux() -> str:
    """First line of ``command -v tmux`` under /bin/sh, or ""."""
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", "command -v tmux"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = proc.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def _first_executable(candidates) -> str:
    """First candidate that is an executable file, or ""."""
    for candidate in candidates:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def _login_shell_tmux() -> str:
    """Ask a login shell where tmux is (its rc files set the real PATH).

    A login shell can hang for ages on a slow host (rc files doing
    network work), so each attempt is bounded; a shell that is not on
    PATH is skipped outright.
    """
    for shell in _LOGIN_SHELLS:
        shell_path = shutil.which(shell)
        if not shell_path:
            continue
        try:
            proc = subprocess.run(
                [shell_path, "-lc", "command -v tmux"],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = proc.stdout.strip().splitlines()
        if not lines or not lines[0].strip():
            continue
        found = lines[0].strip()
        # `command -v` may name a shell function instead of a binary;
        # only a real executable is usable as TMUX_BIN.
        if os.path.isfile(found) and os.access(found, os.X_OK):
            return found
    return ""


class TmuxResolver:
    """Caches the tmux path, probed in the SSH probe's order.

    PATH first (``command -v tmux``), then the well-known locations,
    then a login shell. ``override`` (from ``--tmux`` / config) is used
    as-is and never re-probed. Otherwise the probe runs at startup and
    again whenever a command returns 127, in case tmux appeared (or
    moved) mid-run.
    """

    def __init__(self, override: str | None = None) -> None:
        self.fixed = bool(override)
        self.path = override or ""

    def refresh(self) -> str:
        if self.fixed:
            return self.path
        self.path = (
            _command_v_tmux()
            or _first_executable(TMUX_CANDIDATE_PATHS)
            or _login_shell_tmux()
        )
        return self.path


def probe_tmux() -> str:
    return TmuxResolver().refresh()


# ---------------------------------------------------------------------------
# exec op
# ---------------------------------------------------------------------------

def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the child and anything it spawned (own session: see run_exec)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run_exec(op: dict, resolver: TmuxResolver) -> dict:
    """Run one ``exec`` op. Never raises: failures become error results."""
    command = op.get("command")
    if not isinstance(command, str) or not command:
        return {"ok": False, "error": "exec op needs a non-empty string 'command'"}
    input_b64 = op.get("input_b64", "")
    if not isinstance(input_b64, str):
        return {"ok": False, "error": "exec op 'input_b64' must be a base64 string"}
    try:
        stdin_data = base64.b64decode(input_b64, validate=True) if input_b64 else b""
    except (binascii.Error, ValueError) as exc:
        return {"ok": False, "error": f"invalid input_b64: {exc}"}
    timeout = op.get("timeout", DEFAULT_EXEC_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"invalid timeout: {timeout!r}"}
    if timeout <= 0 or timeout != timeout or timeout in (float("inf"), float("-inf")):
        return {"ok": False, "error": f"invalid timeout: {timeout!r}"}

    env = dict(os.environ)
    env["TMUX_BIN"] = resolver.path

    try:
        proc = subprocess.Popen(
            command, shell=True, executable="/bin/sh",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "error": f"cannot start command: {exc}"}

    chunks: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow: dict[str, bool] = {"stdout": False, "stderr": False}

    def _drain(stream, name: str) -> None:
        # Read until EOF. Past the cap, keep draining (and kill the
        # process) instead of blocking it on a full pipe.
        try:
            while True:
                data = stream.read(65536)
                if not data:
                    break
                if overflow[name]:
                    continue
                if len(chunks[name]) + len(data) > MAX_OUTPUT_BYTES:
                    overflow[name] = True
                    _kill_process_group(proc)
                    continue
                chunks[name] += data
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _feed_stdin() -> None:
        try:
            assert proc.stdin is not None
            view = memoryview(stdin_data)
            while view:
                written = proc.stdin.write(view[:65536])
                if written is None:
                    break
                view = view[written:]
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for thread in readers:
        thread.start()
    writer = threading.Thread(target=_feed_stdin, daemon=True)
    writer.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"command timed out after {timeout:g}s (unkillable)"}
        for thread in readers:
            thread.join(timeout=5)
        return {"ok": False, "error": f"command timed out after {timeout:g}s"}
    for thread in readers:
        thread.join(timeout=5)
    writer.join(timeout=2)

    if overflow["stdout"]:
        return {"ok": False, "error": "stdout exceeded the 16MB limit"}
    if overflow["stderr"]:
        return {"ok": False, "error": "stderr exceeded the 16MB limit"}
    if returncode == 127:
        # A tmux invocation that cannot find tmux exits 127; the binary
        # may have appeared since startup, so re-probe for next time.
        resolver.refresh()
    return {
        "ok": True,
        "stdout_b64": base64.b64encode(bytes(chunks["stdout"])).decode("ascii"),
        "stderr_b64": base64.b64encode(bytes(chunks["stderr"])).decode("ascii"),
        "returncode": returncode,
    }


def run_op(op, resolver: TmuxResolver) -> dict:
    """Run one op from a batch. Unknown ops are errors, never crashes."""
    if not isinstance(op, dict):
        return {"ok": False, "error": "op must be an object"}
    if op.get("op") != "exec":
        return {"ok": False, "error": f"unknown op: {op.get('op')!r}"}
    return run_exec(op, resolver)


def _run_op_guarded(op, resolver: TmuxResolver) -> dict:
    """run_op, with a worker crash turned into an error result.

    run_op/run_exec never raise by contract, but a pool worker raising
    would kill the whole batch reply — one bad op must not do that.
    """
    try:
        return run_op(op, resolver)
    except Exception as exc:  # noqa: BLE001 - see above
        return {"ok": False, "error": f"op failed: {exc}"}


def handle_batch(message: dict, resolver: TmuxResolver) -> dict:
    """Run a batch request; returns the reply dict.

    Ops run in parallel in a thread pool and the results are assembled
    in op order, so a batch of independent reads costs the slowest op,
    not the sum. Each op keeps its own process, pipes and buffers (see
    run_exec), so outputs cannot interleave; the only shared state is
    the resolver's cached tmux path, whose re-probe is idempotent.
    Batches from different messages may overlap (see _MessagePool):
    a single send sequence still cannot overtake itself, because the
    server only sends its next op after the previous reply arrives.
    Replies are sent by the connection's sender thread (see
    WSConnection.send_text), which is the only code that ever writes
    to the socket, so they never interleave on the wire.
    """
    request_id = message.get("id")
    if not isinstance(request_id, str):
        return {"id": "", "ok": False, "error": "request 'id' must be a string"}
    ops = message.get("ops")
    if not isinstance(ops, list):
        return {"id": request_id, "ok": False, "error": "request 'ops' must be a list"}
    if len(ops) <= 1:
        return {"id": request_id, "ok": True,
                "results": [run_op(op, resolver) for op in ops]}
    with ThreadPoolExecutor(max_workers=min(len(ops), _BATCH_MAX_WORKERS)) as pool:
        results = list(pool.map(
            lambda op: _run_op_guarded(op, resolver), ops))
    return {"id": request_id, "ok": True, "results": results}


# ---------------------------------------------------------------------------
# Daemon main loop
# ---------------------------------------------------------------------------

class AuthFailed(Exception):
    """The server closed the connection with 4403: the token is wrong."""


def backoff_delay(failures: int) -> float:
    """1s, 2s, 4s, 8s, 16s, 32s, then capped at 60s."""
    if failures < 0:
        failures = 0
    return min(2.0 ** failures, BACKOFF_CAP)


def server_host(server: str) -> str:
    try:
        return urlparse(server).hostname or server
    except ValueError:
        return server


def _sleep_interruptible(seconds: float, stop_event: threading.Event | None) -> bool:
    """Sleep, returning False promptly if ``stop_event`` is set."""
    if stop_event is None:
        time.sleep(seconds)
        return True
    deadline = time.monotonic() + seconds
    while True:
        if stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.1, remaining))


def _send_reply(conn: WSConnection, reply: dict) -> None:
    """Send a computed reply; on failure, answer with an error result.

    A reply whose send fails must never be dropped silently: the old
    code logged "dropping reply for a message" and moved on, so the
    server-side op waited out its whole timeout instead of failing at
    once. The error reply is best effort — when the connection is
    already gone it fails too, and the server then learns it from the
    disconnect (which fails the pending op immediately instead of
    timing it out). Either way the caller gets an answer, not silence.
    """
    try:
        conn.send_text(json.dumps(reply))
    except Exception as exc:  # noqa: BLE001 - answer, don't drop
        log(f"reply send failed ({exc!r}); attempting error reply")
        try:
            conn.send_text(json.dumps({
                "id": reply.get("id", ""), "ok": False,
                "error": f"agent could not send reply: {exc}"}))
        except Exception as exc2:  # noqa: BLE001 - connection is gone
            log(f"error reply also failed, connection is gone: {exc2!r}")


def _handle_message(conn: WSConnection, text: str, resolver: TmuxResolver) -> None:
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        log(f"ignoring non-JSON message: {exc}")
        return
    if not isinstance(message, dict):
        log("ignoring non-object message")
        return
    if message.get("type") == "welcome":
        who = message.get("host_name") or message.get("host_id") or "?"
        log(f"welcome from host {who}")
        return
    if "ops" in message:
        _send_reply(conn, handle_batch(message, resolver))
        return
    log(f"ignoring unknown message type: {message.get('type')!r}")


class _MessagePool:
    """Bounded pool of daemon threads for concurrent message handling.

    The main loop only reads and dispatches; a worker runs one whole
    message (``_handle_message``, which blocks until its batch is
    done). That is what keeps a slow message — a sweep over dozens of
    panes, a big file read — from holding up the next transcript or
    capture behind it.

    Why daemon threads instead of ``ThreadPoolExecutor``: a worker
    stuck in a slow op must never hold up process exit, and on
    disconnect the queued work must go away at once. ``shutdown()``
    drains the queue (not-yet-started items are dropped) and the
    workers exit after their current item; a worker whose connection
    is already gone gets its reply answered with an error result
    (see _send_reply) or learns it from the disconnect. No deadlock is possible
    between the pools: a message worker only ever waits on its own
    op pool (see ``handle_batch``), never on this pool, and op workers
    never touch this pool.
    """

    def __init__(self, max_workers: int, handler) -> None:
        self._handler = handler
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._run, name=f"tmuxweb-agent-msg-{index}",
                daemon=True)
            thread.start()
            self._threads.append(thread)

    def submit(self, item) -> bool:
        """Queue ``item`` for a worker; False once the pool is closed."""
        with self._lock:
            if self._closed:
                return False
            self._queue.put(item)
            return True

    def shutdown(self) -> None:
        """Stop accepting work and drop everything still queued.

        In-flight items run to their (bounded) op timeout and then the
        workers exit; this call itself never blocks.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        for _ in self._threads:
            self._queue.put(None)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._handler(item)
            except Exception as exc:  # noqa: BLE001 - a worker never dies
                log(f"message worker failed: {exc!r}")


def _serve_connection(conn: WSConnection, token: str, resolver: TmuxResolver,
                      stop_event: threading.Event | None) -> None:
    """Hello + read loop for one connection. Raises on disconnect."""
    hello = {"type": "hello", "token": token,
             "agent": {"version": VERSION, "hostname": socket.gethostname(),
                       "tmux": resolver.path}}
    conn.send_text(json.dumps(hello))

    inbox: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            while True:
                inbox.put(("message", conn.read_message()))
        except ConnectionClosed as exc:
            inbox.put(("closed", exc))
        except Exception as exc:  # noqa: BLE001 - surface it, don't die silently
            inbox.put(("error", exc))

    thread = threading.Thread(target=_reader, name="tmuxweb-agent-reader", daemon=True)
    thread.start()

    # Messages run on the pool (see _MessagePool), so the loop never
    # blocks on a slow batch: the next message is read and dispatched
    # right away and the heartbeat stays on schedule. Replies are sent
    # from the workers through the connection's sender thread (see
    # WSConnection.send_text) and matched to callers by request_id on
    # the server, so arrival order between messages does not matter.
    # The heartbeat may wait behind a big reply in the sender's queue
    # (same as it waited on the old send lock); liveness is the
    # server's ping, not this heartbeat.
    def _dispatch(text: str) -> None:
        try:
            _handle_message(conn, text, resolver)
        except Exception as exc:  # noqa: BLE001 - a send failure is
            # answered with an error reply inside _send_reply; anything
            # reaching here is the batch itself failing, which is logged
            # and never takes the worker (or the pool) down with it.
            log(f"message worker failed: {exc!r}")

    pool = _MessagePool(_MESSAGE_MAX_WORKERS, _dispatch)
    try:
        last_heartbeat = time.monotonic()
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                kind, item = inbox.get(timeout=0.5)
            except queue.Empty:
                kind, item = None, None
            if kind == "closed":
                if item.code == CLOSE_AUTH_FAILED:
                    raise AuthFailed("server rejected the token (close 4403)")
                raise item
            if kind == "error":
                raise item
            if kind == "message":
                if not pool.submit(item):
                    log("dropping message: connection closing")
            if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL:
                conn.send_text(json.dumps({"type": "heartbeat", "at": int(time.time())}))
                last_heartbeat = time.monotonic()
    finally:
        pool.shutdown()


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum, _frame):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            pass  # not the main thread (e.g. under test): nothing to do


def run_daemon(server: str, token: str, *, tmux: str | None = None,
               stop_event: threading.Event | None = None) -> int:
    """Connect, hello, serve, reconnect with backoff until stopped.

    Returns 0 on a clean stop, 3 when the server rejects the token
    (4403: retrying with the same token cannot help).
    """
    if stop_event is None:
        stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    resolver = TmuxResolver(override=tmux)
    resolver.refresh()
    log(f"tmuxweb-agent {VERSION} starting (tmux={resolver.path or 'not found'})")

    failures = 0
    while not stop_event.is_set():
        try:
            conn = WSConnection.connect(server)
        except Exception as exc:
            log(f"connect to {server_host(server)} failed: {exc}")
            if not _sleep_interruptible(backoff_delay(failures), stop_event):
                break
            failures += 1
            continue
        failures = 0
        log(f"connected to {server_host(server)}")
        try:
            _serve_connection(conn, token, resolver, stop_event)
        except AuthFailed as exc:
            log(f"authentication failed: {exc}; not retrying")
            _close_quietly(conn)
            return 3
        except ConnectionClosed as exc:
            log(f"disconnected: {exc}")
        except Exception as exc:  # noqa: BLE001 - reconnect, don't die
            log(f"connection error: {exc!r}")
        finally:
            _close_quietly(conn)
        if stop_event.is_set():
            break
        if not _sleep_interruptible(backoff_delay(failures), stop_event):
            break
        failures += 1
    log("tmuxweb-agent stopped")
    return 0


# ---------------------------------------------------------------------------
# Paths, config, install / status / uninstall
# ---------------------------------------------------------------------------

def agent_dir() -> Path:
    return Path.home() / ".config" / "tmuxweb-agent"


def default_config_path() -> Path:
    return agent_dir() / "config.json"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "tmuxweb-agent.service"


def run_sh_path() -> Path:
    return agent_dir() / "run.sh"


def log_path() -> Path:
    return agent_dir() / "tmuxweb-agent.log"


def pid_path() -> Path:
    return agent_dir() / "tmuxweb-agent.pid"


def loop_pid_path() -> Path:
    return agent_dir() / "tmuxweb-agent-loop.pid"


def python_command() -> str:
    """The python3 to put in generated units/scripts."""
    return shutil.which("python3") or sys.executable


def load_config_file(path: Path) -> dict:
    """Read a JSON config file. Raises ValueError on bad JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid config file {path}: top level must be an object")
    return data


def write_config(path: Path, server: str, token: str, tmux: str) -> None:
    """Write the config (server/token/tmux only) with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(
        {"server": server, "token": token, "tmux": tmux or ""}, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _run_quiet(argv: list[str], timeout: float = 30.0):
    """Run a helper command, swallowing failures; returns CompletedProcess or None."""
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _systemd_user_available() -> bool:
    """True only when a user bus answers -- not just when the binary exists.

    ``systemctl --user daemon-reload`` is a harmless no-op probe: in a
    container without a user bus it fails ("Failed to connect to bus"),
    which is exactly the case where the nohup fallback must be used.
    """
    if shutil.which("systemctl") is None:
        return False
    result = _run_quiet(["systemctl", "--user", "daemon-reload"], timeout=15)
    return result is not None and result.returncode == 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _stop_pid_file(path: Path, timeout: float = 3.0) -> None:
    """SIGTERM, then SIGKILL, one pid from a pid file; removes the file."""
    pid = _read_pid(path)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        path.unlink()
    except OSError:
        pass


def _stop_nohup() -> None:
    """Stop a nohup-installed loop (and its agent child) if one is running."""
    _stop_pid_file(loop_pid_path())   # loop first, so it cannot restart the agent
    _stop_pid_file(pid_path())


def _is_running() -> tuple[bool, str]:
    """(running, detail). Prefers the systemd unit when one is installed."""
    if unit_path().is_file() and _systemd_user_available():
        result = _run_quiet(
            ["systemctl", "--user", "is-active", "--quiet", "tmuxweb-agent.service"],
            timeout=15)
        if result is not None and result.returncode == 0:
            return True, " (systemd)"
        return False, " (systemd)"
    pid = _read_pid(pid_path())
    if pid is not None and _pid_alive(pid):
        return True, f" (pid {pid})"
    loop_pid = _read_pid(loop_pid_path())
    if loop_pid is not None and _pid_alive(loop_pid):
        return True, " (restarting)"
    return False, ""


def _unit_content(agent_abs: str, config_abs: str) -> str:
    return (
        "[Unit]\n"
        "Description=TmuxWeb agent transport\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={python_command()} {agent_abs} --config {config_abs}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _run_sh_content(agent_abs: str, config_abs: str) -> str:
    return (
        "#!/bin/sh\n"
        "# TmuxWeb agent keep-alive loop (no systemd --user here).\n"
        "# Managed by `agent.py install`; re-running install replaces this file.\n"
        f"AGENT=\"{agent_abs}\"\n"
        f"CONFIG=\"{config_abs}\"\n"
        f"PID_FILE=\"{pid_path()}\"\n"
        f"LOG_FILE=\"{log_path()}\"\n"
        f"PYTHON=\"{python_command()}\"\n"
        "delay=1\n"
        "while true; do\n"
        "    \"$PYTHON\" \"$AGENT\" --config \"$CONFIG\" >> \"$LOG_FILE\" 2>&1 &\n"
        "    echo $! > \"$PID_FILE\"\n"
        "    wait $!\n"
        "    sleep \"$delay\"\n"
        "    if [ \"$delay\" -lt 60 ]; then\n"
        "        delay=$((delay * 2))\n"
        "        if [ \"$delay\" -gt 60 ]; then delay=60; fi\n"
        "    fi\n"
        "done\n"
    )


def _wait_for_running(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        running, _ = _is_running()
        if running:
            return True
        time.sleep(0.2)
    running, _ = _is_running()
    return running


def cmd_install(args: argparse.Namespace) -> int:
    server = args.server or os.environ.get("TMUXWEB_AGENT_SERVER") or ""
    token = ""
    if args.token_file:
        try:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"cannot read token file: {exc}", file=sys.stderr)
            return 2
    if not token:
        token = args.token or os.environ.get("TMUXWEB_AGENT_TOKEN") or ""
    if not server:
        print("missing server: pass --server or set TMUXWEB_AGENT_SERVER",
              file=sys.stderr)
        return 2
    if not token:
        print("missing token: pass --token, --token-file, or set TMUXWEB_AGENT_TOKEN",
              file=sys.stderr)
        return 2

    config_path = Path(args.config) if args.config else default_config_path()
    if args.tmux:
        tmux = args.tmux
    else:
        tmux = TmuxResolver().refresh()
        if not tmux:
            # A fresh probe that finds nothing must not wipe a tmux path
            # a previous install (or a manual fix) already recorded:
            # with "" every agent op fails, with the old path the agent
            # keeps working. Only overwrite when the probe finds one.
            try:
                tmux = load_config_file(config_path).get("tmux") or ""
            except (OSError, ValueError):
                tmux = ""
    write_config(config_path, server, token, tmux)
    agent_abs = str(Path(__file__).resolve())
    config_abs = str(config_path.resolve())

    if _systemd_user_available():
        _stop_nohup()  # never leave a nohup copy running next to the unit
        unit = unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(_unit_content(agent_abs, config_abs), encoding="utf-8")
        for argv in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "tmuxweb-agent.service"],
            ["systemctl", "--user", "restart", "tmuxweb-agent.service"],
        ):
            result = _run_quiet(argv, timeout=30)
            if result is None or result.returncode != 0:
                print(f"systemctl failed: {' '.join(argv)}", file=sys.stderr)
                return 1
        print(f"installed (systemd): server={server}")
    else:
        _stop_nohup()  # idempotent: the old loop is gone before the new one starts
        script = run_sh_path()
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(_run_sh_content(agent_abs, config_abs), encoding="utf-8")
        os.chmod(script, 0o755)
        proc = subprocess.Popen(
            ["/bin/sh", str(script)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        loop_pid_path().write_text(str(proc.pid), encoding="utf-8")
        print(f"installed (nohup): server={server}")

    running, detail = _is_running()
    if not running:
        running = _wait_for_running(timeout=5.0)
        _, detail = _is_running()
    print(f"status: {'running' if running else 'not running'}{detail}")
    if not running:
        print("warning: the agent does not appear to be running; "
              "check the log or run `agent.py status`", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else default_config_path()
    server = "(not configured)"
    if config_path.is_file():
        try:
            server = load_config_file(config_path).get("server") or "(not configured)"
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
    running, detail = _is_running()
    print(f"server: {server}")
    print(f"status: {'running' if running else 'not running'}{detail}")
    return 0 if running else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config else default_config_path()
    removed: list[str] = []
    unit = unit_path()
    if unit.is_file():
        if _systemd_user_available():
            _run_quiet(["systemctl", "--user", "disable", "--now",
                        "tmuxweb-agent.service"], timeout=30)
            try:
                unit.unlink()
            except OSError:
                pass
            _run_quiet(["systemctl", "--user", "daemon-reload"], timeout=15)
        else:
            try:
                unit.unlink()
            except OSError:
                pass
        removed.append("unit")
    _stop_nohup()
    for path in (config_path, run_sh_path(), log_path()):
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    for directory in (agent_dir(), unit.parent):
        try:
            directory.rmdir()  # only when already empty
        except OSError:
            pass
    if removed:
        print(f"uninstalled ({', '.join(sorted(set(removed)))})")
    else:
        print("not installed (nothing to remove)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="TmuxWeb agent transport: dial back to the TmuxWeb host "
                    "over WebSocket and run the shell commands it sends.")
    parser.add_argument("--version", action="version", version=f"tmuxweb-agent {VERSION}")
    parser.add_argument("--server", default=None,
                        help="WebSocket URL of the TmuxWeb host, "
                             "e.g. wss://tmux.example.com/agent "
                             "(or TMUXWEB_AGENT_SERVER)")
    parser.add_argument("--token", default=None,
                        help="per-host token (or TMUXWEB_AGENT_TOKEN)")
    parser.add_argument("--config", default=None,
                        help="JSON config file with server/token/tmux "
                             "(default: ~/.config/tmuxweb-agent/config.json)")
    parser.add_argument("--tmux", default=None,
                        help="path to the tmux binary (default: probe with "
                             "`command -v tmux`)")
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="install the agent (systemd --user or nohup)")
    install.add_argument("--server", default=None)
    install.add_argument("--token", default=None, help="token (manual install)")
    install.add_argument("--token-file", default=None,
                         help="read the token from this file, so it never "
                              "appears on a command line")
    install.add_argument("--tmux", default=None)
    install.add_argument("--config", default=None)

    status = sub.add_parser("status", help="show whether the agent is running")
    status.add_argument("--config", default=None)

    uninstall = sub.add_parser("uninstall", help="stop the agent and remove its files")
    uninstall.add_argument("--config", default=None)
    return parser


def _load_daemon_config(args: argparse.Namespace) -> dict:
    """Config for a foreground run: --config if given, else the default path."""
    if args.config:
        path = Path(args.config)
        if not path.is_file():
            print(f"config file not found: {path}", file=sys.stderr)
            raise SystemExit(2)
        try:
            return load_config_file(path)
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            raise SystemExit(2)
    default = default_config_path()
    if default.is_file():
        try:
            return load_config_file(default)
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            raise SystemExit(2)
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "install":
        return cmd_install(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)

    config = _load_daemon_config(args)
    server = args.server or os.environ.get("TMUXWEB_AGENT_SERVER") \
        or config.get("server") or ""
    token = args.token or os.environ.get("TMUXWEB_AGENT_TOKEN") \
        or config.get("token") or ""
    tmux = args.tmux or config.get("tmux") or None
    if not server:
        parser.error("missing server: pass --server, --config, or set "
                     "TMUXWEB_AGENT_SERVER")
    if not token:
        parser.error("missing token: pass --token, --config, or set "
                     "TMUXWEB_AGENT_TOKEN")
    return run_daemon(server, token, tmux=tmux)


if __name__ == "__main__":
    raise SystemExit(main())
