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
* v1.1 adds one more op, ``"needs_input"`` (see the host-side watcher
  below): the server sends ``{"op": "needs_input", "panes": [{"pane_id",
  "provider", "cwd", "title"}]}`` and the agent answers with per-pane
  waiting state ``{"ok": true, "waiting": {"%0": {"waiting": true,
  "source": "hook", "since": <unix ts>, "decision_point": true,
  "control_quiet": true, "hook": true}}, "agent_version": "1.1.0",
  "control_mode": true, "events_seen": 3}``. An agent that does not
  know the op answers with an error result, and the server treats that
  as "no agent signal" (see ``transport.AgentTransport.needs_input_many``)
  — the server-side gate from PR #24 stays the fallback.

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

VERSION = "1.1.0"
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


def run_op(op, resolver: TmuxResolver, watcher: HostWatcher | None = None) -> dict:
    """Run one op from a batch. Unknown ops are errors, never crashes."""
    if not isinstance(op, dict):
        return {"ok": False, "error": "op must be an object"}
    if op.get("op") == "needs_input":
        return run_needs_input(op, resolver, watcher)
    if op.get("op") != "exec":
        return {"ok": False, "error": f"unknown op: {op.get('op')!r}"}
    return run_exec(op, resolver)


def _run_op_guarded(op, resolver: TmuxResolver, watcher: HostWatcher | None = None) -> dict:
    """run_op, with a worker crash turned into an error result.

    run_op/run_exec never raise by contract, but a pool worker raising
    would kill the whole batch reply — one bad op must not do that.
    """
    try:
        return run_op(op, resolver, watcher)
    except Exception as exc:  # noqa: BLE001 - see above
        return {"ok": False, "error": f"op failed: {exc}"}


def handle_batch(message: dict, resolver: TmuxResolver,
                 watcher: HostWatcher | None = None) -> dict:
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
                "results": [run_op(op, resolver, watcher) for op in ops]}
    with ThreadPoolExecutor(max_workers=min(len(ops), _BATCH_MAX_WORKERS)) as pool:
        results = list(pool.map(
            lambda op: _run_op_guarded(op, resolver, watcher), ops))
    return {"id": request_id, "ok": True, "results": results}


# ---------------------------------------------------------------------------
# Host-side "needs you" watcher (stacks on PR #24's server gate)
# ---------------------------------------------------------------------------
# PR #24's server gate is a three-legged conjunction (decision point +
# stable screen + parsed menu) and it is the fallback. This watcher is
# the precision layer that lives on the host, next to the session files
# and the tmux server, so it sees transitions in seconds instead of at
# sweep cadence (~1 minute). It feeds the same gate: the server merges
# the agent-reported waiting state with its own decision point, the
# agent event winning when present.
#
# Three legs, in order of precision:
#
# 1. Hook events (exact). Hook scripts — one shared stdlib script,
#    ``hooks/tmuxweb-hook.py`` — append one JSON line per event to
#    ``~/.config/tmuxweb-agent/events.jsonl`` (next to config.json) and
#    exit 0. The watcher tails that file. A permission event sets
#    waiting; a result/stop event clears it. Hooks never dial out and
#    never block the agent: write + exit 0, short timeout.
# 2. tmux control mode (screen-quiet). A persistent ``tmux -C`` client
#    owned by this program tracks the last ``%output`` time per pane.
#    Quiet + a session-file decision point (leg 3) means the agent is
#    sitting at a prompt it did not announce by hook — question prompts
#    and elicitation, which have no permission event. A dead control
#    client degrades to "no control signal", never kills the agent.
# 3. Session files (decision point). High-frequency mtime polling of
#    the same files ``agent_status.py`` reads on the server (Claude
#    ``~/.claude/projects/<dashified-cwd>/*.jsonl``, Codex
#    ``~/.codex/sessions/**/*.jsonl``, Kimi
#    ``~/.kimi-code/sessions/wd_*/session_*/agents/main/wire.jsonl``),
#    with the same outstanding-call predicates ported here — the agent
#    is deployed standalone and cannot import server modules, so the
#    minimal predicates are copied, not shared.
#
# Hermes has no hook and no usable session-file wait signal (its
# ``verification_required`` finish_reason appears twice in 43314 rows
# and never as a session's latest message), so it stays on the
# server-side gate plus legs 2+3 here only in the sense that leg 3
# cannot fire for it: ``decision_point`` is False for hermes and the
# watcher never reports it waiting. That is fail-closed by design.
#
# Field names are parsed tolerantly on purpose. The verified payloads
# are: Kimi — official docs (kimi.com/code/docs, hooks page): JSON on
# stdin with ``hook_event_name`` / ``session_id`` / ``session_title`` /
# ``cwd``, events ``PermissionRequest`` ("just before waiting for user
# approval"), ``PermissionResult``, ``Stop``; [[hooks]] takes exactly
# ``event`` / ``matcher`` / ``command`` / ``timeout``. Claude — official
# docs (code.claude.com hooks guide): common fields ``session_id`` /
# ``transcript_path`` / ``cwd`` / ``hook_event_name``; ``Notification``
# adds ``message`` / ``title`` / ``notification_type`` with matcher
# ``permission_prompt`` (fires ~6s into the wait) and
# ``elicitation_dialog``; ``Stop`` clears. Codex — no official page was
# reachable from this sandbox; the field names used here
# (``session_id`` / ``transcript_path`` / ``cwd`` /
# ``hook_event_name`` / ``model``, turn events add ``turn_id``,
# ``PermissionRequest`` adds ``tool_name`` / ``tool_input``) are the
# ones several independent sources agree on, and the parser also
# accepts ``sessionId`` / ``thread_id`` aliases, so a rename on
# Codex's side degrades to a cwd match instead of a crash. Where a
# name could not be verified, the hook command itself carries the
# event name as its first argument (see hooks/README.md), so the
# configured event — not a guessed payload field — is authoritative.
# ---------------------------------------------------------------------------

WATCHER_EVENT_TTL_SECONDS = 3600.0  # a permission wait older than this is stale
WATCHER_MAX_EVENTS_BYTES = 256 * 1024  # event file cap; older lines are dropped
WATCHER_MAX_EVENTS_KEPT = 1000
WATCHER_QUIET_SECONDS = 5.0  # no %output for this long == screen quiet
WATCHER_POLL_INTERVAL = 2.0  # background refresh cadence (seconds)
WATCHER_TAIL_BYTES = 65536
# Session-file resolution cache TTL. A pane's session file changes
# identity only when a new session starts in the same cwd — rare
# against the 2s snapshot cadence — so 30s bounds the staleness of
# "which file is newest" while the Codex rglob + first-line scan
# (36ms for 20 files on oracle) runs at most once per pane per 30s
# instead of once per snapshot. The resolved file's mtime is checked
# on every hit, so an appended or replaced file re-resolves at once.
WATCHER_RESOLVE_TTL_SECONDS = 30.0
# A miss (no session file for this pane) is cached only briefly: a
# session file that appears — the pane's agent just started — must
# be found within seconds, not after the full 30s.
WATCHER_RESOLVE_MISS_TTL_SECONDS = 5.0

# Normalised hook-event kinds. "waiting" events set waiting, "clear"
# events clear it. Anything else is ignored (SessionStart, PreToolUse
# and friends carry no wait signal).
_HOOK_WAITING = {
    "permissionprompt", "permissionrequest", "elicitationdialog",
    "agentneedsinput",
}
_HOOK_CLEARING = {
    "permissionresult", "stop", "sessionend", "interrupt",
    "userpromptsubmit", "turnstarted",
}


def _normalise_hook_event(raw) -> str:
    """``PermissionRequest`` / ``permission_prompt`` -> one canonical key."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def parse_hook_event(obj) -> dict | None:
    """One hook payload (any provider) -> a watcher event, or None.

    Tolerant by design: every field has aliases, and a missing
    ``session_id`` falls back to the session id in ``transcript_path``
    (its stem) or to a cwd match. ``event`` is the configured event
    name when the hook command passed one (see hooks/tmuxweb-hook.py);
    otherwise it comes from the payload's own event field, and for a
    Claude ``Notification`` from ``notification_type``.
    """
    if not isinstance(obj, dict):
        return None
    raw_event = (
        obj.get("hook_event_name") or obj.get("event") or obj.get("type")
        or obj.get("notification_type") or ""
    )
    # A Claude Notification names its kind in notification_type, not in
    # the event field ("Notification" itself says nothing): let the
    # kind win whenever it is present.
    if _normalise_hook_event(raw_event) == "notification":
        raw_event = obj.get("notification_type") or raw_event
    kind = _normalise_hook_event(raw_event)
    if kind in _HOOK_WAITING:
        waiting = True
    elif kind in _HOOK_CLEARING:
        waiting = False
    else:
        return None
    session_id = (
        obj.get("session_id") or obj.get("sessionId") or obj.get("thread_id")
        or obj.get("session") or ""
    )
    cwd = obj.get("cwd") or obj.get("working_directory") or ""
    pane_id = obj.get("pane_id") or obj.get("pane") or ""
    transcript = obj.get("transcript_path") or obj.get("transcript") or ""
    if not session_id and transcript:
        stem = os.path.basename(str(transcript))
        session_id = stem[:-6] if stem.endswith(".jsonl") else stem
    try:
        timestamp = float(obj.get("timestamp") or obj.get("at") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {
        "event": raw_event, "waiting": waiting,
        "session_id": str(session_id or ""),
        "cwd": str(cwd or ""), "pane_id": str(pane_id or ""),
        "timestamp": timestamp,
    }


def _watcher_tail_lines(path, tail: int = WATCHER_TAIL_BYTES) -> list[str]:
    """Every complete line in the tail of a session file, oldest first."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - tail))
            chunk = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > tail and lines:
        lines = lines[1:]  # the first line may start mid-record
    return [line.strip() for line in lines if line.strip().endswith("}")]


def _watcher_records(path) -> list[dict]:
    records = []
    for line in _watcher_tail_lines(path):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _watcher_outstanding(provider: str, records) -> bool:
    """Port of agent_status.has_outstanding_call (same rules, stdlib only).

    A tool call was issued and no result has come back — the state a
    permission prompt lives in, because the turn-end marker never
    fires there (0/4008 in real Claude files; see needs_input.py).
    """
    if provider == "claude":
        pending: set = set()
        for event in records:
            kind = event.get("type")
            if kind == "assistant":
                content = (event.get("message") or {}).get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict) and block.get("type") == "tool_use"
                                and block.get("id")):
                            pending.add(block["id"])
            elif kind == "user":
                content = (event.get("message") or {}).get("content")
                blocks = content if isinstance(content, list) else []
                results = [b for b in blocks
                           if isinstance(b, dict) and b.get("type") == "tool_result"]
                if results:
                    for block in results:
                        pending.discard(block.get("tool_use_id"))
                elif blocks or isinstance(content, str):
                    pending.clear()
            elif kind == "system" and event.get("subtype") in (
                    "turn_duration", "stop_hook_summary"):
                pending.clear()
        return bool(pending)
    if provider == "codex":
        pending = set()
        for event in records:
            if event.get("type") == "event_msg":
                if (event.get("payload") or {}).get("type") in (
                        "task_complete", "turn_aborted", "task_started"):
                    pending.clear()
                continue
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload") or {}
            key = payload.get("call_id") or payload.get("id")
            if not key:
                continue
            if payload.get("type") in ("function_call", "custom_tool_call"):
                pending.add(key)
            elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
                pending.discard(key)
        return bool(pending)
    if provider == "kimi":
        pending = False
        for event in records:
            kind = event.get("type")
            if kind == "tool.call":
                pending = True
            elif kind in ("prompt.completed", "turn.ended", "step.end",
                          "prompt.submitted", "llm.request"):
                pending = False
            elif kind == "context.append_loop_event":
                if (event.get("event") or {}).get("type") == "step.end":
                    pending = False
        return pending
    return False


# Turn-end markers, same strings agent_status.IDLE_MARKERS uses.
_WATCHER_IDLE_MARKERS = {
    "claude": ('"subtype":"turn_duration"', '"subtype":"stop_hook_summary"'),
    "codex": ('"type":"task_complete"', '"type":"turn_aborted"'),
    "kimi": ('"type":"prompt.completed"', '"type":"turn.ended"'),
}
_WATCHER_BUSY_MARKERS = {
    "claude": ('"type":"user"', '"type":"assistant"'),
    "codex": ('"type":"task_started"', '"type":"item_completed"', '"type":"token_count"'),
    "kimi": ('"type":"prompt.submitted"', '"type":"llm.request"',
             '"type":"llm.response"', '"type":"step.start"', '"type":"step.end"',
             '"type":"tool.call"', '"type":"agent.message.appended"'),
}
_TIGHTEN = __import__("re").compile(r'"[ \t]*:[ \t]*"')


def _watcher_status_for_file(provider: str, path) -> str:
    """busy / idle / unknown for one session file (agent_status.classify_lines)."""
    if provider not in _WATCHER_IDLE_MARKERS:
        return "unknown"
    for record in reversed(_watcher_tail_lines(path)):
        tightened = _TIGHTEN.sub('":"', record)
        if any(m in tightened for m in _WATCHER_IDLE_MARKERS[provider]):
            return "idle"
        if any(m in tightened for m in _WATCHER_BUSY_MARKERS[provider]):
            return "busy"
    return "unknown"


def _watcher_decision_point(provider: str, path) -> bool:
    """Turn ended, or a tool call outstanding — the PR #24 leg-1 signal."""
    if provider not in _WATCHER_IDLE_MARKERS:
        return False
    if _watcher_status_for_file(provider, path) == "idle":
        return True
    try:
        return _watcher_outstanding(provider, _watcher_records(path))
    except OSError:
        return False


def _dashify_cwd(cwd: str) -> str:
    return __import__("re").sub(r"[^A-Za-z0-9]", "-", cwd or "")


def _watcher_resolve_session_file(provider: str, cwd: str, title: str = "",
                                  hint: str = "", home=None):
    """Locate a pane's session file, same rules as transcript_reader.py.

    Claude: ``~/.claude/projects/<dashified-cwd>/*.jsonl`` (newest; a
    ``hint`` session id wins via glob). Codex:
    ``~/.codex/sessions/**/*.jsonl`` (a ``hint`` in the file name wins,
    else the newest file whose first record is a ``session_meta`` with
    this cwd). Kimi: ``~/.kimi-code/sessions/wd_*/session_*/state.json``
    with this cwd, newest ``updatedAt``, wire file
    ``agents/main/wire.jsonl``. None when it cannot be found.
    """
    home = Path(home) if home is not None else Path.home()
    provider = (provider or "").lower()
    try:
        if provider == "claude":
            projects = home / ".claude" / "projects"
            if hint:
                matches = sorted(projects.glob("*/%s.jsonl" % hint))
                if matches:
                    return matches[0]
            project_dir = projects / _dashify_cwd(cwd)
            candidates = (sorted(project_dir.glob("*.jsonl"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
                          if project_dir.is_dir() else [])
            return candidates[0] if candidates else None
        if provider == "codex":
            files = list((home / ".codex" / "sessions").rglob("*.jsonl")) \
                if (home / ".codex" / "sessions").is_dir() else []
            if hint:
                matches = [p for p in files if hint in p.name]
                if matches:
                    return max(matches, key=lambda p: p.stat().st_mtime)
            for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                try:
                    with path.open(errors="replace") as handle:
                        first = json.loads(handle.readline())
                except (OSError, ValueError):
                    continue
                payload = first.get("payload") or {}
                if first.get("type") == "session_meta" and payload.get("cwd") == cwd:
                    return path
            return None
        if provider == "kimi":
            candidates = []
            root = home / ".kimi-code" / "sessions"
            for state_path in root.glob("wd_*/session_*/state.json") if root.is_dir() else []:
                try:
                    state = json.loads(state_path.read_text(errors="replace"))
                except (OSError, ValueError):
                    continue
                if state.get("archived") or state.get("cwd") != cwd:
                    continue
                wire_path = state_path.parent / "agents" / "main" / "wire.jsonl"
                if not wire_path.is_file():
                    continue
                stamp = state.get("updatedAt") or state.get("createdAt") or 0
                try:
                    stamp = float(stamp) / 1000 if float(stamp) > 10 ** 11 else float(stamp)
                except (TypeError, ValueError):
                    stamp = 0.0
                candidates.append((stamp, wire_path))
            if not candidates:
                return None
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
    except OSError:
        return None
    return None


def _tmux_argv(binary: str, *args: str) -> list[str]:
    """tmux invocation, honouring a non-default socket for tests.

    ``TMUXWEB_AGENT_TMUX_SOCKET`` is a test seam (and a legitimate
    escape hatch on hosts whose tmux server does not live on the
    default socket): when set, every tmux call the watcher makes uses
    ``-S <socket>``. Unset, tmux uses its default socket as usual.
    """
    argv = [binary]
    socket_path = os.environ.get("TMUXWEB_AGENT_TMUX_SOCKET", "")
    if socket_path:
        argv += ["-S", socket_path]
    argv += list(args)
    return argv


class _ControlModeClient:
    """One persistent ``tmux -C`` client, tracking last-%output per pane.

    ``%output %N <text>`` is the only line that matters: it proves the
    pane's screen changed at that moment. Everything else (%begin /
    %end / %session-changed / command replies) is ignored. The client
    only starts when the host actually has tmux sessions — starting
    ``tmux -C`` on a host with no server would create a stray session.
    A dead client degrades to "no control signal" and is restarted
    lazily (at most once per 10s); it never raises into the daemon.
    """

    _RESTART_MIN_INTERVAL = 10.0

    def __init__(self, tmux_path: str = "") -> None:
        self._tmux_path = tmux_path
        self._proc = None
        self._reader = None
        self._lock = threading.Lock()
        self._last_output: dict[str, float] = {}
        self._attached_session: str | None = None
        self._attached_at: float = 0.0
        self._last_start_attempt: float = 0.0
        self._started_ok: bool = False

    # -- parsing (pure: unit-testable without tmux) ----------------------
    def handle_line(self, line: str, now: float | None = None) -> None:
        """Record one control-mode line. ``%output %N ...`` updates %N."""
        if not line.startswith("%output "):
            return
        parts = line.split(" ", 2)
        if len(parts) < 2 or not parts[1].startswith("%"):
            return
        with self._lock:
            self._last_output[parts[1]] = time.time() if now is None else now

    def quiet_seconds(self, pane_id: str, now: float | None = None,
                      session: str | None = None) -> float | None:
        """Seconds since this pane last produced output, or None.

        None means "unknown": the client is not attached to this pane's
        session (or never started), so quiet cannot be judged — the
        caller must not treat that as quiet. ``session`` is the pane's
        session from the watcher's list-panes map; when it is given,
        a pane in any other session reads unknown even if a stale
        ``%output`` entry for it survives from an earlier attachment
        (the client only ever sees output for the session it is
        attached to, so an entry for another session's pane is
        unobserved since — it must not read as quiet). An entry older
        than the current attach likewise counts as "no output while
        attached": quiet since the attach, not since the stale entry.
        Without ``session`` (direct unit-test use) the old behaviour
        holds: a recorded entry wins, else quiet since attach.
        """
        now = time.time() if now is None else now
        with self._lock:
            last = self._last_output.get(pane_id)
            attached_at = self._attached_at
            attached = self._attached_session
        if session is not None:
            if attached is None or attached != session:
                return None
            if last is not None and last >= attached_at:
                return max(0.0, now - last)
            if attached_at:
                return max(0.0, now - attached_at)
            return None
        if last is not None:
            return max(0.0, now - last)
        if attached is not None and attached_at:
            return max(0.0, now - attached_at)
        return None

    @property
    def available(self) -> bool:
        return self._started_ok and self._proc is not None and self._proc.poll() is None

    @property
    def attached_session(self):
        return self._attached_session

    # -- lifecycle ---------------------------------------------------------
    def _reader_loop(self) -> None:
        try:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                self.handle_line(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            self._started_ok = False

    def ensure_started(self, tmux_path: str = "") -> bool:
        """Start the client if it is not running. Never raises."""
        if self.available:
            return True
        now = time.monotonic()
        if now - self._last_start_attempt < self._RESTART_MIN_INTERVAL:
            return False
        self._last_start_attempt = now
        binary = tmux_path or self._tmux_path
        if not binary:
            return False
        try:
            # Only start when sessions exist: `tmux -C` on a host with
            # no server would create a stray session of its own.
            probe = subprocess.run(
                _tmux_argv(binary, "list-sessions", "-F", "#{session_name}"),
                capture_output=True, text=True, timeout=5)
            if probe.returncode != 0 or not probe.stdout.strip():
                return False
            proc = subprocess.Popen(
                _tmux_argv(binary, "-C"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except (OSError, subprocess.SubprocessError):
            return False
        self._proc = proc
        self._attached_session = None
        self._attached_at = 0.0
        self._started_ok = True
        self._reader = threading.Thread(target=self._reader_loop,
                                        name="tmuxweb-agent-control-mode",
                                        daemon=True)
        self._reader.start()
        return True

    def attach(self, session_name: str) -> bool:
        """Attach the control client to a session (quoted, no shell)."""
        if not self.available or not session_name:
            return False
        if self._attached_session == session_name:
            return True
        try:
            assert self._proc is not None and self._proc.stdin is not None
            import shlex as _shlex
            self._proc.stdin.write(
                "attach-session -t %s\n" % _shlex.quote(session_name))
            self._proc.stdin.flush()
        except (OSError, ValueError, BrokenPipeError):
            return False
        self._attached_session = session_name
        self._attached_at = time.time()
        return True

    def stop(self) -> None:
        proc, reader = self._proc, self._reader
        self._proc = None
        self._started_ok = False
        self._attached_session = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write("detach-client\n")
                    proc.stdin.flush()
            except (OSError, ValueError, BrokenPipeError):
                pass
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                except OSError:
                    pass
        if reader is not None:
            reader.join(timeout=2)


class HostWatcher:
    """Per-pane "needs you" state from hooks + control mode + session files.

    The watcher is cheap to construct and safe to use without its
    background threads: :meth:`snapshot` computes fresh when its
    caches are cold, so a one-shot ``needs_input`` op is fresh even
    if the poll thread never ran. :meth:`start` adds the background
    loop (event tail + control-mode attach + session mtimes every
    :attr:`poll_interval`) so %output times stay warm between server
    sweeps. Every failure mode degrades to "no signal", never to a
    crash: a missing event file, a dead control client, an
    unresolvable session file and an unknown provider all read as
    "not waiting".

    Caching (all fail-open to a fresh read, never to a stale "yes"):
    the snapshot result is cached for one poll interval and keyed on
    the pane set, so a ``needs_input`` op arriving just after the
    poll loop reuses it; session-file resolution is cached per
    (provider, cwd, hint) for 30s (5s for a miss) and invalidated
    when the resolved file's mtime changes; the event file is only
    re-parsed when its mtime/size changed, with the 1h TTL applied
    at use time. Session-file *contents* are never cached — every
    uncached snapshot re-reads the tail — only the "which file"
    answer and the parsed events are.
    """

    def __init__(self, resolver: TmuxResolver, *, home=None, events_path=None,
                 poll_interval: float = WATCHER_POLL_INTERVAL,
                 event_ttl: float = WATCHER_EVENT_TTL_SECONDS,
                 quiet_seconds: float = WATCHER_QUIET_SECONDS) -> None:
        self._resolver = resolver
        self._home = Path(home) if home is not None else Path.home()
        if events_path is not None:
            self._events_path = Path(events_path)
        else:
            self._events_path = (self._home / ".config" / "tmuxweb-agent"
                                 / "events.jsonl")
        self._poll_interval = max(0.5, float(poll_interval))
        # Snapshot cache freshness window: one poll interval. A
        # needs_input op arriving shortly after the poll loop's
        # snapshot reuses it instead of recomputing; a result older
        # than this is never served. 2s of staleness is the accepted
        # bound — the hook leg (exact, second-scale) and the server
        # gate (the fallback) both cover the gap, and a one-shot op
        # with a cold cache still computes fresh.
        self._snapshot_ttl = self._poll_interval
        self._event_ttl = float(event_ttl)
        self._quiet_seconds = float(quiet_seconds)
        self._lock = threading.Lock()
        self._known: dict[str, dict] = {}  # pane_id -> descriptor
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._control = _ControlModeClient(resolver.path if resolver else "")
        self._pane_sessions: dict[str, str] = {}  # pane_id -> session name
        self._pane_sessions_at: float = 0.0
        self._events_seen: int = 0
        # Snapshot cache: (pane-set key, result, monotonic timestamp).
        self._snapshot_cache: tuple | None = None
        # Resolution cache: (provider, cwd, hint) ->
        # (path or None, monotonic timestamp, resolved mtime_ns or None).
        self._resolve_cache: dict = {}
        # Event cache: (parsed events, (mtime_ns, size) signature).
        # The TTL is applied at use time, not at parse time, so a
        # reused parse still expires events on schedule.
        self._events_cache: list[dict] | None = None
        self._events_sig: tuple | None = None

    # -- event file --------------------------------------------------------
    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def events_seen(self) -> int:
        return self._events_seen

    def _read_events(self, now: float | None = None) -> list[dict]:
        """Parsed, unexpired hook events, oldest first.

        The file is re-parsed only when its (mtime_ns, size) changed
        since the last read — the poll loop and the op path both call
        this every ~2s, and re-parsing up to 1000 lines each time is
        the cost this avoids. The 1h TTL is applied at use time, so
        a reused parse still lets events expire on schedule; a
        deleted file reads as no events (the cache is dropped).
        """
        now = time.time() if now is None else now
        try:
            stat = self._events_path.stat()
            sig = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._events_cache = None
            self._events_sig = None
            self._events_seen = 0
            return []
        if sig != self._events_sig or self._events_cache is None:
            try:
                if stat.st_size > WATCHER_MAX_EVENTS_BYTES:
                    self._rotate_events()
                    stat = self._events_path.stat()
                    sig = (stat.st_mtime_ns, stat.st_size)
                text = self._events_path.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                self._events_cache = None
                self._events_sig = None
                self._events_seen = 0
                return []
            parsed = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = parse_hook_event(json.loads(line))
                except ValueError:
                    continue
                if event is None:
                    continue
                parsed.append(event)
            self._events_cache = parsed
            self._events_sig = sig
        events = [e for e in (self._events_cache or [])
                  if now - e["timestamp"] <= self._event_ttl]
        self._events_seen = len(events)
        return events

    def _rotate_events(self) -> None:
        """Keep only the newest lines when the file grows past the cap."""
        try:
            lines = self._events_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
            kept = lines[-WATCHER_MAX_EVENTS_KEPT:]
            tmp = self._events_path.with_name(self._events_path.name + ".tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                           encoding="utf-8")
            os.replace(tmp, self._events_path)
        except OSError:
            pass

    def _hook_state(self, pane: dict, events, now: float) -> tuple[bool, float | None]:
        """(waiting, since) from hook events matching this pane.

        A clearing event after the last waiting event wins; events are
        matched by session id, then cwd, then pane id — in that order,
        because a session id is exact and a cwd is shared by panes.
        """
        session_id = (pane.get("session_id") or "").strip()
        cwd = (pane.get("cwd") or "").strip()
        pane_id = (pane.get("pane_id") or "").strip()
        waiting_since: float | None = None
        for event in events:
            if session_id and event["session_id"] == session_id:
                match = True
            elif cwd and event["cwd"] == cwd:
                match = True
            elif pane_id and event["pane_id"] == pane_id:
                match = True
            else:
                match = False
            if not match:
                continue
            if event["waiting"]:
                if waiting_since is None:
                    waiting_since = event["timestamp"]
            else:
                waiting_since = None
        if waiting_since is None:
            return False, None
        return True, waiting_since

    # -- session files -------------------------------------------------------
    def _session_file(self, pane: dict):
        """A pane's session file, cached per (provider, cwd, hint).

        The resolver ignores ``title``, so it is not part of the key.
        A hit is used only while its TTL holds (30s for a found file,
        5s for a miss) and the resolved file's mtime is unchanged —
        an appended or replaced file re-resolves at once. A brand-new
        session file superseding the cached one for the same cwd is
        picked up when the TTL expires (at most 30s late); the hook
        leg and the server gate cover that window.
        """
        provider = (pane.get("provider") or "").lower()
        cwd = pane.get("cwd") or ""
        hint = pane.get("session_id") or ""
        key = (provider, cwd, hint)
        now_mono = time.monotonic()
        entry = self._resolve_cache.get(key)
        if entry is not None:
            path, cached_at, mtime_ns = entry
            ttl = (WATCHER_RESOLVE_TTL_SECONDS if path is not None
                   else WATCHER_RESOLVE_MISS_TTL_SECONDS)
            if now_mono - cached_at < ttl:
                if path is None:
                    return None
                try:
                    if path.stat().st_mtime_ns == mtime_ns:
                        return path
                except OSError:
                    pass  # the file vanished: re-resolve below
        path = _watcher_resolve_session_file(
            provider, cwd, pane.get("title") or "", hint, home=self._home)
        resolved_mtime = None
        if path is not None:
            try:
                resolved_mtime = path.stat().st_mtime_ns
            except OSError:
                resolved_mtime = None
        self._resolve_cache[key] = (path, now_mono, resolved_mtime)
        return path

    def _decision_point(self, pane: dict) -> tuple[bool, str | None]:
        """(decision_point, session_id) — the file leg, fail-closed."""
        path = self._session_file(pane)
        if path is None:
            return False, None
        provider = (pane.get("provider") or "").lower()
        return _watcher_decision_point(provider, path), path.stem

    # -- control mode --------------------------------------------------------
    def _refresh_pane_sessions(self) -> None:
        """pane_id -> session name, via one list-panes (cached 30s)."""
        now = time.monotonic()
        if now - self._pane_sessions_at < 30 and self._pane_sessions:
            return
        binary = self._resolver.path if self._resolver else ""
        if not binary:
            return
        try:
            proc = subprocess.run(
                _tmux_argv(binary, "list-panes", "-a", "-F", "#{pane_id} #{session_name}"),
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return
        if proc.returncode != 0:
            return
        mapping = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].startswith("%"):
                mapping[parts[0]] = parts[1]
        self._pane_sessions = mapping
        self._pane_sessions_at = now

    def _control_quiet(self, pane: dict, now: float) -> bool:
        """True when control mode proves this pane's screen is quiet.

        The pane's session comes from the cached list-panes map, and
        quiet is judged against that session only: a pane whose
        session is unknown to the map reads unknown (False), never
        quiet — a stale ``%output`` entry for it must not light a
        card for a session the client never observed.
        """
        binary = self._resolver.path if self._resolver else ""
        if not self._control.ensure_started(binary):
            return False
        self._refresh_pane_sessions()
        session = self._pane_sessions.get(pane.get("pane_id") or "")
        if not session:
            return False
        self._control.attach(session)
        quiet = self._control.quiet_seconds(pane.get("pane_id") or "", now,
                                            session=session)
        return quiet is not None and quiet >= self._quiet_seconds

    # -- snapshot --------------------------------------------------------------
    def snapshot(self, panes, now: float | None = None) -> dict:
        """Per-pane waiting state for the server's ``needs_input`` op.

        ``panes`` is a list of ``{"pane_id", "provider", "cwd", "title",
        "session_id"?}``. Waiting is: a hook event says so (exact), OR
        the session file is at a decision point AND control mode says
        the screen is quiet (inferred — question prompts and
        elicitation, which carry no permission event). A hook event
        alone is enough; a decision point alone is not — that is the
        server gate's job, and this watcher must not duplicate its
        false-positive mode.

        The result is cached for one poll interval, keyed on the pane
        set, so a ``needs_input`` op arriving just after the poll
        loop's snapshot reuses it instead of recomputing. A different
        pane list, or a result older than the window, recomputes; a
        cold cache (the poll thread never ran) computes fresh.
        """
        now = time.time() if now is None else now
        normalized = []
        for raw in panes or []:
            if not isinstance(raw, dict):
                continue
            pane_id = str(raw.get("pane_id") or raw.get("id") or "")
            if not pane_id:
                continue
            normalized.append({
                "pane_id": pane_id,
                "provider": str(raw.get("provider") or "").lower(),
                "cwd": str(raw.get("cwd") or raw.get("path") or ""),
                "title": str(raw.get("title") or ""),
                "session_id": str(raw.get("session_id") or ""),
            })
        key = tuple(sorted(
            (p["pane_id"], p["provider"], p["cwd"], p["title"],
             p["session_id"]) for p in normalized))
        cached = self._snapshot_cache
        if (cached is not None and cached[0] == key
                and time.monotonic() - cached[2] < self._snapshot_ttl):
            return {pid: dict(state) for pid, state in cached[1].items()}
        events = self._read_events(now)
        out: dict[str, dict] = {}
        for pane in normalized:
            pane_id = pane["pane_id"]
            with self._lock:
                self._known[pane_id] = pane
            hook_waiting, hook_since = self._hook_state(pane, events, now)
            decision, file_session = self._decision_point(pane)
            if not pane["session_id"] and file_session:
                pane["session_id"] = file_session
                hook_waiting, hook_since = self._hook_state(pane, events, now)
            control_quiet = self._control_quiet(pane, now) if decision else False
            if hook_waiting:
                waiting, source, since = True, "hook", hook_since
            elif decision and control_quiet:
                waiting, source, since = True, "control-mode", now - self._quiet_seconds
            else:
                waiting, source, since = False, None, None
            out[pane_id] = {
                "waiting": waiting, "source": source, "since": since,
                "decision_point": decision, "control_quiet": control_quiet,
                "hook": hook_waiting,
            }
        self._snapshot_cache = (key, out, time.monotonic())
        return out

    @property
    def control_mode_available(self) -> bool:
        return self._control.available

    # -- background loop -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="tmuxweb-agent-watcher",
                                        daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    panes = list(self._known.values())
                if panes:
                    self.snapshot(panes)
            except Exception:  # noqa: BLE001 - the watcher never kills the agent
                log("host watcher poll failed (continuing)")
            self._stop.wait(self._poll_interval)

    def stop(self) -> None:
        self._stop.set()
        self._control.stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
            self._thread = None


def run_needs_input(op: dict, resolver: TmuxResolver,
                    watcher: HostWatcher | None = None) -> dict:
    """Answer one ``needs_input`` op. Never raises: failures become errors."""
    panes = op.get("panes")
    if not isinstance(panes, list):
        return {"ok": False, "error": "needs_input op needs a list 'panes'"}
    own_watcher = watcher is None
    if own_watcher:
        watcher = HostWatcher(resolver)
    assert watcher is not None
    try:
        waiting = watcher.snapshot(panes)
    except Exception as exc:  # noqa: BLE001 - the op answers, never crashes
        return {"ok": False, "error": f"needs_input failed: {exc}"}
    finally:
        if own_watcher:
            watcher.stop()
    return {"ok": True, "waiting": waiting, "agent_version": VERSION,
            "control_mode": watcher.control_mode_available,
            "events_seen": watcher.events_seen}


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


def _handle_message(conn: WSConnection, text: str, resolver: TmuxResolver,
                    watcher: HostWatcher | None = None) -> None:
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
        _send_reply(conn, handle_batch(message, resolver, watcher))
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
                      stop_event: threading.Event | None,
                      watcher: HostWatcher | None = None) -> None:
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
            _handle_message(conn, text, resolver, watcher)
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

    # The host watcher lives for the life of the daemon, across
    # reconnects: its hook state and control-mode client survive a
    # dropped WebSocket, and it stops only when the daemon stops.
    watcher = HostWatcher(resolver)
    watcher.start()
    failures = 0
    try:
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
                _serve_connection(conn, token, resolver, stop_event, watcher)
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
    finally:
        watcher.stop()
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


def events_path() -> Path:
    """Hook events land here (see the host watcher in this file)."""
    return agent_dir() / "events.jsonl"


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
    for path in (config_path, run_sh_path(), log_path(), events_path()):
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
