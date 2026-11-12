"""The agent transport: run remote commands over a WebSocket agent program.

The agent (``plugins/agent/agent.py``, a single stdlib-only file on the
remote host) calls back to ``/ws/agent`` and answers batched ``exec`` ops.
This module is the server side of that deal:

* :class:`AgentRegistry` — the live connections, one per host, plus the
  per-host status the console shows (online / last seen / last transport).
* :class:`AgentTransport` — an :class:`SSHRunner` that tries the agent
  first and falls back to SSH when the agent is not there.

Two failure kinds, kept apart on purpose:

* :class:`AgentUnavailable` — there is no live connection, or the
  connection died while sending. The command (probably) never ran, so
  falling back to SSH and running it there is safe.
* a plain ``RuntimeError`` on timeout *while the connection is still
  alive* — the command may already be running on the far side, so it
  must NOT be re-run over SSH. The caller sees the timeout instead.

``call`` is deliberately synchronous: every caller runs in a worker
thread (``asyncio.to_thread``), and the request is marshalled onto the
connection's event loop with ``run_coroutine_threadsafe``. Calling it
from the connection's own loop raises instead of deadlocking.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import logging
import shlex
import subprocess
import threading
import time
import uuid

from remote_hosts import ANSI_RE, PANE_ID_RE, SSHRunner

LOGGER = logging.getLogger("tmuxweb.agent")

# The protocol only has one op in v1; both sides check, so a newer agent
# cannot smuggle an unexpected op past the server (or vice versa).
ALLOWED_OPS = ("exec",)


class AgentUnavailable(RuntimeError):
    """No live agent connection (or it died mid-send): falling back is safe."""


class _Connection:
    """One live agent WebSocket, plus the futures waiting on its replies."""

    __slots__ = ("host_id", "websocket", "loop", "pending", "connected_at",
                 "last_seen", "alive")

    def __init__(self, host_id: str, websocket) -> None:
        self.host_id = host_id
        self.websocket = websocket
        self.loop = asyncio.get_running_loop()
        self.pending: dict[str, asyncio.Future] = {}
        now = time.time()
        self.connected_at = now
        self.last_seen = now
        self.alive = True


class AgentRegistry:
    """Live agent connections, indexed by host id.

    Status (``last_seen`` / ``last_transport`` / ``last_transport_at``) is
    kept in a separate per-host record that survives disconnects, so the
    console can still say "agent, last seen 5 minutes ago, last transport
    ssh" after the agent went away.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[str, _Connection] = {}
        self._records: dict[str, dict] = {}

    # -- connections ------------------------------------------------------
    def register(self, host_id: str, websocket) -> _Connection:
        """Remember a new connection, closing any older one for this host.

        Only one live connection per host: the old one is closed and its
        waiting callers get :class:`AgentUnavailable` instead of an answer
        from a connection that no longer owns the host.
        """
        connection = _Connection(host_id, websocket)
        with self._lock:
            old = self._connections.pop(host_id, None)
            self._connections[host_id] = connection
            record = self._records.setdefault(host_id, {})
            record["connected_at"] = connection.connected_at
            record["last_seen"] = connection.last_seen
        if old is not None:
            self._close(old)
        return connection

    def unregister(self, host_id: str, connection: _Connection) -> None:
        """Drop a connection, but only if it is still the current one."""
        with self._lock:
            if self._connections.get(host_id) is connection:
                del self._connections[host_id]
        self._close(connection)

    def _close(self, connection: _Connection) -> None:
        connection.alive = False
        for future in list(connection.pending.values()):
            if not future.done():
                connection.loop.call_soon_threadsafe(
                    future.set_exception,
                    AgentUnavailable("agent connection closed"))
        connection.pending.clear()
        if connection.loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                connection.websocket.close(), connection.loop)
        except RuntimeError:
            pass

    def disconnect(self, host_id: str) -> bool:
        """Drop (and close) the live connection for a host, if there is one."""
        with self._lock:
            connection = self._connections.pop(host_id, None)
        if connection is None:
            return False
        self._close(connection)
        return True

    def shutdown(self) -> None:
        """Drop every live connection (server shutdown)."""
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            self._close(connection)

    def connection(self, host_id: str) -> _Connection | None:
        with self._lock:
            return self._connections.get(host_id)

    def is_online(self, host_id: str) -> bool:
        connection = self.connection(host_id)
        return connection is not None and connection.alive

    # -- status ------------------------------------------------------------
    def touch(self, host_id: str, at: float | None = None) -> None:
        """Any message from the agent proves it is alive right now."""
        now = float(at) if at is not None else time.time()
        with self._lock:
            record = self._records.setdefault(host_id, {})
            record["last_seen"] = now
            connection = self._connections.get(host_id)
            if connection is not None:
                connection.last_seen = now

    def note_transport(self, host_id: str, kind: str) -> None:
        """Remember which transport carried the last operation for a host."""
        with self._lock:
            record = self._records.setdefault(host_id, {})
            record["last_transport"] = kind
            record["last_transport_at"] = time.time()

    def status(self, host_id: str) -> dict:
        """``{agent_online, agent_last_seen, last_transport, last_transport_at}``.

        Unknown hosts get the all-default shape, so callers never have to
        special-case a host that never had an agent.
        """
        with self._lock:
            record = dict(self._records.get(host_id) or {})
            connection = self._connections.get(host_id)
        return {
            "agent_online": bool(connection is not None and connection.alive),
            "agent_last_seen": record.get("last_seen"),
            "last_transport": record.get("last_transport"),
            "last_transport_at": record.get("last_transport_at"),
        }

    def deliver(self, host_id: str, request_id: str, message: dict) -> bool:
        """Hand a reply packet to the future waiting for ``request_id``."""
        connection = self.connection(host_id)
        if connection is None:
            return False
        future = connection.pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    # -- calls --------------------------------------------------------------
    def call(self, host_id: str, ops: list[dict], timeout: float = 15) -> dict:
        """Send one batch and wait for its reply packet (synchronous).

        ``ops`` is validated against the v1 allowlist. Raises
        :class:`AgentUnavailable` when there is no live connection (or it
        dies), and ``RuntimeError`` when the agent stays connected but does
        not answer within ``timeout`` — the caller must not retry that one.
        """
        if not ops or not isinstance(ops, list):
            raise ValueError("ops must be a non-empty list")
        for op in ops:
            if not isinstance(op, dict) or op.get("op") not in ALLOWED_OPS:
                raise ValueError("unsupported op: %r" % (op.get("op") if isinstance(op, dict) else op,))
            if not isinstance(op.get("command"), str):
                raise ValueError("exec op needs a string command")
        connection = self.connection(host_id)
        if connection is None or not connection.alive:
            raise AgentUnavailable("no agent connected for host %s" % host_id)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is connection.loop:
            raise RuntimeError(
                "AgentRegistry.call must not run on the agent connection loop;"
                " call it from a worker thread (asyncio.to_thread)")
        request_id = uuid.uuid4().hex
        coro = self._do_call(connection, request_id, ops, float(timeout))
        try:
            return asyncio.run_coroutine_threadsafe(
                coro, connection.loop).result(float(timeout) + 5)
        except concurrent.futures.TimeoutError as exc:
            # The loop itself is stuck: the request may have been sent, so
            # this is a timeout, not an "unavailable" — never fall back.
            raise RuntimeError(
                "agent %s did not answer within %ss" % (host_id, timeout)) from exc

    async def _do_call(self, connection: _Connection, request_id: str,
                       ops: list[dict], timeout: float) -> dict:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        connection.pending[request_id] = future
        try:
            try:
                await connection.websocket.send_json({"id": request_id, "ops": ops})
            except Exception as exc:                    # send on a dead socket
                raise AgentUnavailable(
                    "agent connection broke while sending: %s" % exc) from exc
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout)
            except asyncio.TimeoutError as exc:
                if connection.alive and self.connection(connection.host_id) is connection:
                    raise RuntimeError(
                        "agent %s did not answer within %ss" % (connection.host_id, timeout)) from exc
                raise AgentUnavailable(
                    "agent %s went away while waiting for a reply" % connection.host_id) from exc
        finally:
            connection.pending.pop(request_id, None)


class AgentTransport(SSHRunner):
    """An SSHRunner that runs commands through the agent when it is online.

    Everything except :meth:`run`, :meth:`run_many`,
    :meth:`capture_with_pane` and :meth:`stream_file` is inherited:
    ``tmux_binary`` / ``check`` / ``list_panes`` / ``capture`` / ``send``
    and the rest all go through :meth:`run`, so they pick the transport
    up for free. ``spawn`` stays SSH-only on purpose — it is only used by
    the SSH ``stream_file``, which this class overrides.
    """

    def __init__(self, host: dict, registry: AgentRegistry) -> None:
        super().__init__(host)
        self.registry = registry

    def run(self, remote_command: str, *, input_bytes: bytes | None = None,
            timeout: int = 15, resolve_tmux: bool = True) -> subprocess.CompletedProcess:
        host_id = str(self.host.get("id") or "")
        op: dict = {"op": "exec", "command": remote_command, "timeout": timeout}
        if input_bytes is not None:
            op["input_b64"] = base64.b64encode(input_bytes).decode()
        try:
            packet = self.registry.call(host_id, [op], timeout=timeout)
        except AgentUnavailable:
            self.registry.note_transport(host_id, "ssh")
            return super().run(remote_command, input_bytes=input_bytes,
                               timeout=timeout, resolve_tmux=resolve_tmux)
        # A timeout (RuntimeError) propagates: the command may already have
        # run on the far side, so it must not be re-run over SSH.
        self.registry.note_transport(host_id, "agent")
        if not isinstance(packet, dict) or not packet.get("ok"):
            error = packet.get("error") if isinstance(packet, dict) else None
            raise RuntimeError(str(error or "agent returned an invalid response"))
        results = packet.get("results") or []
        if len(results) != 1:
            raise RuntimeError("agent returned %d results for 1 op" % len(results))
        result = results[0]
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else None
            raise RuntimeError(str(error or "agent command failed"))
        stdout = base64.b64decode(b"".join((result.get("stdout_b64") or "").encode().split()))
        stderr = base64.b64decode(b"".join((result.get("stderr_b64") or "").encode().split()))
        return subprocess.CompletedProcess(args=remote_command,
                                           returncode=int(result.get("returncode", 0)),
                                           stdout=stdout, stderr=stderr)

    def run_many(self, remote_commands, *, timeout: int = 15,
                 resolve_tmux: bool = True) -> list[subprocess.CompletedProcess]:
        """Run several commands in a single agent round trip.

        One :meth:`AgentRegistry.call` carries every op, so N commands cost
        one round trip instead of N. The results come back in order, one
        ``CompletedProcess`` per command. Batches carry no stdin.

        A per-op failure is a ``CompletedProcess`` with returncode 1 and the
        agent's error in stderr (never an exception), so callers can treat a
        failed op like a failed command. The batch itself failing (no reply,
        wrong shape) raises like :meth:`run` does, and the
        AgentUnavailable / timeout split is the same: no live agent falls
        back to SSH per command, a timeout while connected never does.
        ``timeout`` budgets the whole batch reply and each op's own kill
        deadline, same as :meth:`run`.
        """
        commands = [str(command) for command in remote_commands]
        if not commands:
            return []
        host_id = str(self.host.get("id") or "")
        ops = [{"op": "exec", "command": command, "timeout": timeout}
               for command in commands]
        try:
            packet = self.registry.call(host_id, ops, timeout=timeout)
        except AgentUnavailable:
            self.registry.note_transport(host_id, "ssh")
            # Plain loop, not a comprehension: zero-arg super() inside a
            # comprehension binds to the comprehension's implicit argument
            # on Python <= 3.11 (TypeError), so it must stay outside one.
            fallback = []
            for command in commands:
                fallback.append(super().run(command, timeout=timeout,
                                            resolve_tmux=resolve_tmux))
            return fallback
        # A timeout (RuntimeError) propagates: commands may already be
        # running on the far side, so they must not be re-run over SSH.
        self.registry.note_transport(host_id, "agent")
        if not isinstance(packet, dict) or not packet.get("ok"):
            error = packet.get("error") if isinstance(packet, dict) else None
            raise RuntimeError(str(error or "agent returned an invalid response"))
        results = packet.get("results") or []
        if len(results) != len(commands):
            raise RuntimeError("agent returned %d results for %d ops"
                               % (len(results), len(commands)))
        return [self._decode_result(command, result)
                for command, result in zip(commands, results)]

    @staticmethod
    def _decode_result(command: str, result) -> subprocess.CompletedProcess:
        """One batch result -> CompletedProcess; an op error becomes rc 1."""
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else None
            return subprocess.CompletedProcess(
                args=command, returncode=1, stdout=b"",
                stderr=str(error or "agent command failed").encode())
        stdout = base64.b64decode(b"".join((result.get("stdout_b64") or "").encode().split()))
        stderr = base64.b64decode(b"".join((result.get("stderr_b64") or "").encode().split()))
        return subprocess.CompletedProcess(
            args=command, returncode=int(result.get("returncode", 0)),
            stdout=stdout, stderr=stderr)

    def capture_and_status_many(self, pane_ids, history: int = 40,
                                status_ids=None, panes: dict | None = None) -> tuple[dict, dict]:
        """Capture panes and ask their session files in one agent round trip.

        The sweep needs both per host per cycle; sending the two scripts
        as one batch costs one round trip instead of two. ``status_ids``
        is the subset with a provider (only those have a session file to
        ask); omit it and only the capture runs. Returns
        ``(captures, statuses)``, parsed exactly like :meth:`capture_many`
        and :meth:`status_many` parse them, so a host answers the same
        over either transport. A failed capture op raises RuntimeError,
        like ``capture_many``; a failed status op just yields no statuses,
        like ``status_many``. AgentUnavailable falls back to the two SSH
        runs (see :meth:`run_many`); a timeout while connected propagates,
        never re-run over SSH.
        """
        cap_script = self._capture_many_script(pane_ids, history)
        status_script = self._status_many_script(status_ids) if status_ids else None
        commands = [script for script in (cap_script, status_script)
                    if script is not None]
        if not commands:
            return {}, {}
        results = self.run_many(commands, timeout=self.capture_timeout or 15)
        # run_many returns one CompletedProcess per command, in order.
        captured = results[0]
        if captured.returncode != 0 and not captured.stdout:
            raise RuntimeError(captured.stderr.decode(errors="replace").strip()
                               or "remote capture failed")
        captures = self._parse_capture_many_output(
            captured.stdout.decode(errors="replace"))
        statuses = {}
        if status_script is not None:
            statuses = self._parse_status_many_output(
                results[1].stdout.decode(errors="replace"))
        return captures, statuses

    def capture_with_pane(self, pane_id: str, history: int = 500) -> tuple[dict, str]:
        """List panes and capture one pane in a single agent round trip.

        The capture endpoint used to pay for two: ``list_panes`` to find the
        pane, then ``capture``. This sends both commands in one batch and
        splits the answers apart, so a capture costs one round trip. The two
        commands are the same ones :meth:`list_panes` and
        :meth:`capture_ansi` build, and the answers are judged the same way.

        Returns ``(pane, text)``. Raises ``KeyError`` when the pane is not
        in the listing (the endpoint turns that into a 404) and
        ``RuntimeError`` when either command fails.
        """
        if not PANE_ID_RE.fullmatch(pane_id):
            raise ValueError("invalid pane id")
        listed, captured = self.run_many(
            [self._list_panes_command(),
             self._capture_ansi_command(pane_id, history)],
            timeout=self.capture_timeout or 15)
        if listed.returncode != 0:
            raise RuntimeError(listed.stderr.decode(errors="replace").strip()
                               or "remote tmux failed")
        panes = self._parse_list_output(listed.stdout.decode(errors="replace"))
        for pane in panes:
            if pane.get("pane_id") == pane_id:
                if captured.returncode != 0:
                    raise RuntimeError(captured.stderr.decode(errors="replace").strip()
                                       or "remote capture failed")
                return pane, ANSI_RE.sub("", captured.stdout.decode(errors="replace"))
        raise KeyError(pane_id)

    def stream_file(self, path: str, max_bytes: int, chunk: int = 65536,
                    idle_timeout: float = 90.0):
        """Read a remote file in 1 MB blocks through the agent.

        One ``exec`` per block (``dd`` + ``base64``), never ``spawn`` — that
        one is SSH-only. A block that comes back short is the last block;
        a first block that fails means the file is not there.
        """
        if "\x00" in path or "\n" in path:
            raise ValueError("invalid path")
        quoted = shlex.quote(path)
        # An empty file and a missing file both read back zero bytes, so
        # check first (same exit-44 shape as the SSH stream_file): without
        # this a missing file would silently download as an empty one.
        exists = self.run(
            f"if [ ! -f {quoted} ]; then echo 'no such file: {path}' >&2; exit 44; fi",
            timeout=max(15, int(idle_timeout)))
        if exists.returncode != 0:
            raise RuntimeError(
                exists.stderr.decode(errors="replace").strip() or "no such file: %s" % path)
        block = 1024 * 1024
        sent = 0
        index = 0
        while True:
            command = (f"dd if={quoted} bs=1M skip={index} count=1 2>/dev/null | base64")
            completed = self.run(command, timeout=max(15, int(idle_timeout)))
            if completed.returncode != 0 and not completed.stdout:
                if index == 0:
                    raise RuntimeError("no such file: %s" % path)
                break
            raw = base64.b64decode(b"".join(completed.stdout.split()))
            if not raw:
                break
            sent += len(raw)
            if sent > max_bytes:
                raise RuntimeError("file is larger than the download limit")
            for start in range(0, len(raw), max(1, chunk)):
                yield raw[start:start + chunk]
            if len(raw) < block:
                break
            index += 1
