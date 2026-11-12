"""Agent transport: tokens, the /ws/agent handshake, exec round-trips,
SSH fallback, reconnect, timeout, one-click install, and the core wiring.

The fake agent talks to the real app over a real socket: each test boots
the app once and serves it with uvicorn on 127.0.0.1, then drives a
``websockets`` client in its own thread — the same shape the real agent
program will use. Plain HTTP goes through TestClient on the same app
object, so tokens issued over HTTP verify on the socket server.
"""

import asyncio
import base64
import json
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "agent"))

import websockets  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import remote_hosts  # noqa: E402
import transport as agent_transport  # noqa: E402
from remote_hosts import RemoteHostStore, SSHRunner  # noqa: E402
from test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux  # noqa: E402
from tmux_web import create_app  # noqa: E402

PASSWORD = "agent-test-password"
AGENT_DIR = Path(__file__).resolve().parent.parent / "plugins" / "agent"


def _ok(data: bytes) -> dict:
    return {"ok": True, "stdout_b64": base64.b64encode(data).decode(),
            "stderr_b64": "", "returncode": 0}


def _close_code(exc) -> int | None:
    """The close code the peer sent (``.code`` is deprecated in websockets)."""
    received = getattr(exc, "rcvd", None)
    if received is not None:
        return received.code
    return getattr(exc, "code", None)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeWS:
    """A stand-in WebSocket for registry unit tests (no server involved)."""

    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed = True


class FakeAgent:
    """A fake agent program: connects, says hello, answers exec ops.

    The default exec handler pretends to be tmux: ``list-panes`` returns
    one canned pane, ``capture-pane`` returns the prompt box (with the
    pasted text until a C-m lands, empty after), ``load-buffer`` stores
    the pasted text, ``dd`` reads from ``self.files``. Pass
    ``silent=True`` for an agent that receives but never answers.
    """

    def __init__(self, ws_url, token, *, silent=False, exec_handler=None):
        self.ws_url = ws_url
        self.token = token
        self.silent = silent
        self.exec_handler = exec_handler
        self.received = []
        self.welcome = None
        self.pasted = ""
        self.submitted = False
        self.files = {}
        self.ready = threading.Event()
        self.closed = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._ws = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        try:
            async with websockets.connect(self.ws_url, open_timeout=5) as ws:
                self._ws = ws
                await ws.send(json.dumps({
                    "type": "hello", "token": self.token,
                    "agent": {"version": "1.0.0", "hostname": "fake",
                              "tmux": "/usr/bin/tmux"}}))
                try:
                    self.welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except Exception:
                    return
                finally:
                    self.ready.set()
                await ws.send(json.dumps({"type": "heartbeat", "at": time.time()}))
                async for message in ws:
                    data = json.loads(message)
                    self.received.append(data)
                    if self.silent:
                        continue
                    reply = self._reply(data)
                    if reply is not None:
                        await ws.send(json.dumps(reply))
        except Exception:
            pass
        finally:
            self.ready.set()
            self.closed.set()

    def _reply(self, data):
        results = []
        for op in data.get("ops") or []:
            if op.get("op") != "exec":
                results.append({"ok": False, "error": "unsupported op"})
                continue
            handler = self.exec_handler or self._default_exec
            results.append(handler(op))
        return {"id": data.get("id"), "ok": True, "results": results}

    def _default_exec(self, op):
        command = op["command"]
        payload = (base64.b64decode(op.get("input_b64") or "")
                   if op.get("input_b64") else b"")
        if "load-buffer" in command:
            self.pasted = payload.decode(errors="replace")
            self.submitted = False
            return _ok(b"")
        if "list-panes" in command:
            fields = ["%9", "remote:0.0", "remote", "0", "agent", "0", "claude",
                      "Remote Claude", "/srv/work", "1", "100", "30", "0", "1234"]
            return _ok(("<::>".join(fields) + "\n__TMUXWEB_PROCESS_TABLE__\n").encode())
        if "capture-pane" in command:
            screen = ("old output\n❯ \n" if self.submitted
                      else "old output\n❯ %s\n" % self.pasted)
            return _ok(screen.encode())
        if "send-keys" in command and "C-m" in command:
            self.submitted = True
            return _ok(b"")
        if command.startswith("dd if="):
            first = command.split("|")[0]
            parts = shlex.split(first)
            path = next((item[3:] for item in parts if item.startswith("if=")), "")
            match = re.search(r"skip=(\d+)", command)
            index = int(match.group(1)) if match else 0
            data = self.files.get(path, b"")
            return _ok(base64.b64encode(data[index * 1024 * 1024:(index + 1) * 1024 * 1024]))
        if command.startswith("if [ ! -f "):
            # the stream_file existence probe: 44 when the file is not there
            match = re.search(r"\[ ! -f (\S+)", command)
            path = match.group(1).strip("'\"") if match else ""
            if path not in self.files:
                return {"ok": True, "stdout_b64": "",
                        "stderr_b64": base64.b64encode(
                            ("no such file: %s" % path).encode()).decode(),
                        "returncode": 44}
            return _ok(b"")
        return _ok(("out:" + command).encode())

    def start(self):
        self._thread.start()
        if not self.ready.wait(timeout=10):
            raise AssertionError("fake agent never finished its hello")

    def stop(self):
        ws = self._ws
        if ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), self._loop)
            except RuntimeError:
                pass
        self.closed.wait(timeout=5)
        self._thread.join(timeout=5)


class _UvicornServer:
    def _start_server(self, app):
        import uvicorn

        self._port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self._port, log_level="error")
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(target=self._server.run, daemon=True)
        self._server_thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        self.ws_url = "ws://127.0.0.1:%d/ws/agent" % self._port

    def _stop_server(self):
        server = getattr(self, "_server", None)
        if server is not None:
            server.should_exit = True
            self._server_thread.join(timeout=5)


class AgentTokenStoreTests(unittest.TestCase):
    """The token lifecycle on the real RemoteHostStore (no app involved)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = RemoteHostStore(Path(self.dir.name) / "hosts.db",
                                     Path(self.dir.name) / "key")
        self.host = self.store.add_host(
            name="h", hostname="10.0.0.9", port=22, username="u",
            auth_type="agent", credential="", host_key="", fingerprint="")
        self.host_id = self.host["id"]

    def test_issue_then_verify_then_revoke(self):
        token = self.store.issue_agent_token(self.host_id)
        self.assertEqual(self.store.verify_agent_token(token), self.host_id)
        self.assertIsNone(self.store.verify_agent_token("wrong-token"))
        self.assertIsNone(self.store.verify_agent_token(""))
        info = self.store.agent_token_info(self.host_id)
        self.assertIsNotNone(info)
        self.assertIsNone(info["last_seen"])
        self.store.touch_agent_last_seen(self.host_id, 1234.5)
        self.assertEqual(self.store.agent_token_info(self.host_id)["last_seen"], 1234.5)
        self.assertTrue(self.store.revoke_agent_token(self.host_id))
        self.assertIsNone(self.store.verify_agent_token(token))
        self.assertIsNone(self.store.agent_token_info(self.host_id))
        self.assertFalse(self.store.revoke_agent_token(self.host_id))

    def test_rotation_invalidates_the_old_token_at_once(self):
        old = self.store.issue_agent_token(self.host_id)
        new = self.store.issue_agent_token(self.host_id)
        self.assertNotEqual(old, new)
        self.assertIsNone(self.store.verify_agent_token(old))
        self.assertEqual(self.store.verify_agent_token(new), self.host_id)

    def test_issue_for_an_unknown_host_raises(self):
        with self.assertRaises(KeyError):
            self.store.issue_agent_token("0000000000000000")

    def test_transport_defaults_to_ssh_and_rejects_anything_else(self):
        self.assertEqual(self.host["transport"], "ssh")
        agent_host = self.store.add_host(
            name="a", hostname="10.0.0.10", port=22, username="u",
            auth_type="agent", credential="", host_key="", fingerprint="",
            transport="agent")
        self.assertEqual(agent_host["transport"], "agent")
        with self.assertRaises(ValueError):
            self.store.add_host(
                name="b", hostname="10.0.0.11", port=22, username="u",
                auth_type="agent", credential="", host_key="", fingerprint="",
                transport="pigeon")
        updated = self.store.update_host(self.host_id, transport="agent")
        self.assertEqual(updated["transport"], "agent")
        with self.assertRaises(ValueError):
            self.store.update_host(self.host_id, transport="pigeon")
        self.assertEqual(self.store.get_host(self.host_id)["transport"], "agent")

    def test_migration_adds_transport_to_a_database_from_before(self):
        from cryptography.fernet import Fernet

        db_path = Path(self.dir.name) / "old.db"
        key_path = Path(self.dir.name) / "old.key"
        key_path.write_bytes(Fernet.generate_key())
        fernet = Fernet(key_path.read_bytes())
        connection = sqlite3.connect(str(db_path))
        connection.execute(
            "CREATE TABLE remote_hosts (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " hostname TEXT NOT NULL, port INTEGER NOT NULL, username TEXT NOT NULL,"
            " auth_type TEXT NOT NULL, credential BLOB NOT NULL, host_key TEXT NOT NULL,"
            " fingerprint TEXT NOT NULL, created_at REAL NOT NULL,"
            " enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0,"
            " jump_host_id TEXT NOT NULL DEFAULT '', jump_spec TEXT NOT NULL DEFAULT '',"
            " tmux_path TEXT NOT NULL DEFAULT '')")
        connection.execute(
            "INSERT INTO remote_hosts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("aabbccddeeff0011", "old", "10.0.0.9", 22, "u", "agent",
             fernet.encrypt(b""), "", "", 0.0, 1, 0, "", "", ""))
        connection.commit()
        connection.close()
        reopened = RemoteHostStore(db_path, key_path)
        self.assertEqual(reopened.get_host("aabbccddeeff0011")["transport"], "ssh")


class AgentRegistryUnitTests(unittest.TestCase):
    """The registry on its own: no server, no socket."""

    def setUp(self):
        self.registry = agent_transport.AgentRegistry()

    def test_call_without_a_connection_is_unavailable(self):
        with self.assertRaises(agent_transport.AgentUnavailable):
            self.registry.call("nope", [{"op": "exec", "command": "echo hi"}])

    def test_call_rejects_anything_but_exec(self):
        with self.assertRaises(ValueError):
            self.registry.call("nope", [{"op": "rm", "command": "x"}])
        with self.assertRaises(ValueError):
            self.registry.call("nope", [{"op": "exec"}])

    def test_status_defaults_for_an_unknown_host(self):
        self.assertEqual(self.registry.status("nope"), {
            "agent_online": False, "agent_last_seen": None,
            "last_transport": None, "last_transport_at": None})

    def test_call_from_the_connections_own_loop_raises(self):
        ws = _FakeWS()
        loop = asyncio.new_event_loop()

        async def _go():
            self.registry.register("h1", ws)
            with self.assertRaises(RuntimeError):
                self.registry.call("h1", [{"op": "exec", "command": "echo hi"}], timeout=2)
            self.registry.disconnect("h1")
            await asyncio.sleep(0.05)

        loop.run_until_complete(_go())
        loop.close()
        self.assertTrue(ws.closed)

    def test_register_replaces_the_old_connection(self):
        first, second = _FakeWS(), _FakeWS()
        loop = asyncio.new_event_loop()

        async def _go():
            self.registry.register("h1", first)
            current = self.registry.register("h1", second)
            await asyncio.sleep(0.05)
            self.registry.unregister("h1", current)
            await asyncio.sleep(0.05)

        loop.run_until_complete(_go())
        loop.close()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertFalse(self.registry.is_online("h1"))


class AgentTransportTests(unittest.TestCase, _UvicornServer):
    """End to end against the real app, served on a real socket."""

    def setUp(self):
        self.tmux = FakeTmux()
        self.store = FakeRemoteStore()
        self.app = create_app(
            self.tmux, password=PASSWORD, session_secret="agent-test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:",
            home_db=":memory:", remote_store=self.store,
            remote_runner_factory=FakeSSHRunner,
            plugin_state=str(Path(tempfile.mkdtemp()) / "plugins.json"))
        self.client = TestClient(self.app)
        self.client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.host = self.store.add_host(
            name="agent-host", hostname="10.0.0.9", port=22, username="ubuntu",
            auth_type="agent", credential="", host_key="", fingerprint="",
            transport="agent")
        self.host_id = self.host["id"]
        self.host2 = self.store.add_host(
            name="second", hostname="10.0.0.10", port=22, username="ubuntu",
            auth_type="agent", credential="", host_key="", fingerprint="")
        self.plugin = self.app.state.plugins.loaded["agent"]
        self.registry = self.plugin.registry
        # The tests pretend this host's Tailscale IPv4 is fixed, so install
        # and token issue always resolve a tailnet address; the no-tailnet
        # tests unset it (and mock discovery) per test.
        self.plugin.tailscale_ip = "100.123.123.5"
        self._hello_timeout = self.plugin.hello_timeout
        self._agents = []
        FakeSSHRunner.run_calls.clear()
        FakeSSHRunner.upload_calls.clear()
        self._start_server(self.app)

    def tearDown(self):
        for agent in self._agents:
            agent.stop()
        self.plugin.hello_timeout = self._hello_timeout
        self._stop_server()
        remote_hosts._TMUX_BIN_CACHE.clear()
        remote_hosts._TMUX_BIN_FAILED.clear()
        FakeSSHRunner.run_calls.clear()
        FakeSSHRunner.upload_calls.clear()

    # -- helpers -----------------------------------------------------------
    def _issue_token(self, host_id=None):
        response = self.client.post("/api/hosts/%s/agent-token" % (host_id or self.host_id))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def _connect_agent(self, token=None, **kwargs):
        agent = FakeAgent(self.ws_url, token or self._issue_token(), **kwargs)
        agent.start()
        self._agents.append(agent)
        return agent

    def _wait_online(self, host_id, online, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.registry.is_online(host_id) == online:
                return True
            time.sleep(0.05)
        return self.registry.is_online(host_id) == online

    def _transport(self, host=None):
        host = host or self.store.get_host(self.host_id, include_credential=True)
        return agent_transport.AgentTransport(host, self.registry)

    # -- plugin surface ------------------------------------------------------
    def test_plugin_is_listed_enabled_and_first_party(self):
        rows = {row["name"]: row for row in self.client.get("/api/plugins").json()["plugins"]}
        self.assertIn("agent", rows)
        self.assertTrue(rows["agent"]["enabled"])
        self.assertTrue(rows["agent"]["first_party"])
        self.assertEqual(rows["agent"]["error"], "")

    def test_runner_factory_returns_ssh_for_ssh_hosts_and_agent_for_agent_hosts(self):
        ssh_host = self.store.get_host(self.host2["id"])
        agent_host = self.store.get_host(self.host_id)
        self.assertIsInstance(self.plugin.runner_factory(ssh_host), SSHRunner)
        self.assertNotIsInstance(self.plugin.runner_factory(ssh_host),
                                 agent_transport.AgentTransport)
        self.assertIsInstance(self.plugin.runner_factory(agent_host),
                              agent_transport.AgentTransport)

    # -- tokens ---------------------------------------------------------------
    def test_token_http_issue_verify_rotate_revoke(self):
        first = self._issue_token()
        self.assertEqual(self.store.verify_agent_token(first), self.host_id)
        second = self._issue_token()
        self.assertNotEqual(first, second)
        self.assertIsNone(self.store.verify_agent_token(first))
        self.assertEqual(self.store.verify_agent_token(second), self.host_id)
        response = self.client.delete("/api/hosts/%s/agent-token" % self.host_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.store.verify_agent_token(second))

    def test_token_issue_logs_never_contain_the_plaintext_token(self):
        with self.assertLogs(level="INFO") as captured:
            token = self._issue_token()
        self.assertNotIn(token, "\n".join(captured.output))
        # ...and the stored value is a hash, not the token itself
        self.assertNotIn(token, self.store.agent_tokens[self.host_id]["token_hash"])

    def test_token_endpoints_need_a_host_and_a_login(self):
        self.assertEqual(
            self.client.post("/api/hosts/0000000000000000/agent-token").status_code, 404)
        anonymous = TestClient(self.app)
        self.assertEqual(
            anonymous.post("/api/hosts/%s/agent-token" % self.host_id).status_code, 401)

    # -- the websocket handshake ----------------------------------------------
    def test_ws_welcome_registers_the_host(self):
        agent = self._connect_agent()
        self.assertEqual(agent.welcome["host_id"], self.host_id)
        self.assertEqual(agent.welcome["host_name"], "agent-host")
        self.assertTrue(self.registry.is_online(self.host_id))
        status = self.client.get("/api/hosts/%s/agent-status" % self.host_id).json()
        self.assertEqual(status["transport"], "agent")
        self.assertTrue(status["agent_online"])
        self.assertIsNotNone(status["agent_last_seen"])

    def test_ws_wrong_token_gets_4403(self):
        async def _bad():
            async with websockets.connect(self.ws_url, open_timeout=5) as ws:
                await ws.send(json.dumps({"type": "hello", "token": "wrong",
                                          "agent": {}}))
                try:
                    await ws.recv()
                except websockets.exceptions.ConnectionClosed as exc:
                    return _close_code(exc)
                return None

        self.assertEqual(_run(_bad()), 4403)
        self.assertFalse(self.registry.is_online(self.host_id))

    def test_ws_no_hello_disconnects_quickly(self):
        self.plugin.hello_timeout = 0.5

        async def _silent():
            async with websockets.connect(self.ws_url, open_timeout=5) as ws:
                try:
                    await ws.recv()
                except websockets.exceptions.ConnectionClosed as exc:
                    return _close_code(exc)
                return None

        started = time.time()
        self.assertEqual(_run(_silent()), 4408)
        self.assertLess(time.time() - started, 5)

    def test_a_token_for_one_host_cannot_register_another(self):
        token = self._issue_token(self.host_id)
        agent = self._connect_agent(token=token)
        self.assertEqual(agent.welcome["host_id"], self.host_id)
        self.assertTrue(self.registry.is_online(self.host_id))
        self.assertFalse(self.registry.is_online(self.host2["id"]))

    def test_revoke_disconnects_the_live_agent(self):
        token = self._issue_token()
        agent = self._connect_agent(token=token)
        self.assertTrue(self._wait_online(self.host_id, True))
        response = self.client.delete("/api/hosts/%s/agent-token" % self.host_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(agent.closed.wait(timeout=5), "revoke did not close the agent socket")
        self.assertTrue(self._wait_online(self.host_id, False))

    # -- operations through the agent -----------------------------------------
    def test_exec_round_trip_and_a_two_op_batch(self):
        agent = self._connect_agent()
        self.assertEqual(agent.welcome["host_id"], self.host_id)
        result = self._transport().run("printf hello", timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"out:printf hello")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(agent.received[-1]["ops"][0]["op"], "exec")
        packet = self.registry.call(
            self.host_id,
            [{"op": "exec", "command": "one"}, {"op": "exec", "command": "two"}],
            timeout=5)
        self.assertTrue(packet["ok"])
        self.assertEqual(
            [base64.b64decode(item["stdout_b64"]) for item in packet["results"]],
            [b"out:one", b"out:two"])

    def test_list_capture_send_go_through_the_agent(self):
        agent = self._connect_agent()
        transport = self._transport()
        panes = transport.list_panes()
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]["pane_id"], "%9")
        self.assertEqual(panes[0]["session"], "remote")
        self.assertEqual(panes[0]["command"], "claude")
        self.assertIn("old output", transport.capture("%9"))
        self.assertTrue(transport.send("%9", "hello agent world", submit=True))
        pasted = [op for packet in agent.received for op in packet["ops"]
                  if "load-buffer" in op["command"]]
        self.assertEqual(len(pasted), 1)
        self.assertEqual(base64.b64decode(pasted[0]["input_b64"]), b"hello agent world")
        self.assertTrue(any("send-keys" in op["command"] and "C-m" in op["command"]
                            for packet in agent.received for op in packet["ops"]))

    def test_stream_file_reads_in_blocks_through_the_agent(self):
        agent = self._connect_agent()
        data = bytes(range(256)) * 10000          # 2.56 MB, so three 1 MB blocks
        agent.files["/home/fake/big.bin"] = data
        chunks = list(self._transport().stream_file("/home/fake/big.bin", 10 * 1024 * 1024))
        self.assertEqual(b"".join(chunks), data)
        dd_calls = [op["command"] for packet in agent.received for op in packet["ops"]
                    if op["command"].startswith("dd if=")]
        self.assertEqual(len(dd_calls), 3)
        with self.assertRaises(RuntimeError):
            list(self._transport().stream_file("/home/fake/nope.bin", 1024))

    # -- fallback and reconnect -----------------------------------------------
    def test_fallback_to_ssh_after_the_agent_disconnects(self):
        agent = self._connect_agent()
        self.assertTrue(self._wait_online(self.host_id, True))
        agent.stop()
        self.assertTrue(self._wait_online(self.host_id, False))
        calls = []

        def fake_ssh_run(self, remote_command, *, input_bytes=None, timeout=15,
                         resolve_tmux=True):
            calls.append(remote_command)
            return subprocess.CompletedProcess(args=remote_command, returncode=0,
                                               stdout=b"via-ssh", stderr=b"")

        with mock.patch.object(SSHRunner, "run", fake_ssh_run):
            result = self._transport().run("echo hi", timeout=5)
        self.assertEqual(result.stdout, b"via-ssh")
        self.assertEqual(calls, ["echo hi"])
        status = self.registry.status(self.host_id)
        self.assertFalse(status["agent_online"])
        self.assertEqual(status["last_transport"], "ssh")

    def test_agent_transport_without_an_agent_falls_back_to_ssh_at_once(self):
        calls = []

        def fake_ssh_run(self, remote_command, *, input_bytes=None, timeout=15,
                         resolve_tmux=True):
            calls.append(remote_command)
            return subprocess.CompletedProcess(args=remote_command, returncode=0,
                                               stdout=b"via-ssh", stderr=b"")

        with mock.patch.object(SSHRunner, "run", fake_ssh_run):
            result = self._transport().run("echo hi", timeout=5)
        self.assertEqual(result.stdout, b"via-ssh")
        self.assertEqual(calls, ["echo hi"])
        self.assertEqual(self.registry.status(self.host_id)["last_transport"], "ssh")

    def test_reconnect_restores_the_agent_transport(self):
        agent = self._connect_agent()
        transport = self._transport()
        self.assertEqual(transport.run("echo one", timeout=5).stdout, b"out:echo one")
        agent.stop()
        self.assertTrue(self._wait_online(self.host_id, False))
        second = self._connect_agent()
        self.assertEqual(transport.run("echo two", timeout=5).stdout, b"out:echo two")
        self.assertTrue(second.received)

    def test_a_silent_agent_times_out_without_falling_back(self):
        self._connect_agent(silent=True)
        self.assertTrue(self._wait_online(self.host_id, True))

        def boom(self, *args, **kwargs):
            raise AssertionError("a timeout must never fall back to SSH")

        started = time.time()
        with mock.patch.object(SSHRunner, "run", boom):
            with self.assertRaises(RuntimeError):
                self._transport().run("echo hi", timeout=2)
        self.assertLess(time.time() - started, 10)

    # -- one-click install ------------------------------------------------------
    def test_install_pushes_the_program_and_the_token_only_as_stdin(self):
        issued = {}
        original_issue = self.store.issue_agent_token

        def spy(host_id):
            token = original_issue(host_id)
            issued["token"] = token
            return token

        module = sys.modules["tmuxweb_plugin_agent"]
        original_factory = module._ssh_runner_factory
        FakeSSHRunner.run_calls.clear()
        FakeSSHRunner.upload_calls.clear()
        try:
            module._ssh_runner_factory = FakeSSHRunner
            with mock.patch.object(self.store, "issue_agent_token", spy):
                response = self.client.post("/api/hosts/%s/agent-install" % self.host_id)
        finally:
            module._ssh_runner_factory = original_factory
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "installed", "transport": "agent"})
        token = issued["token"]
        commands = [command for command, _ in FakeSSHRunner.run_calls]
        inputs = [data for _, data in FakeSSHRunner.run_calls]
        self.assertIn(token.encode(), inputs)                    # token went in as stdin
        self.assertFalse(any(token in command for command in commands))
        self.assertFalse(any(token.encode() in data
                              for _, data in FakeSSHRunner.upload_calls))
        self.assertTrue(any("ws://100.123.123.5:8787/ws/agent" in command for command in commands))
        self.assertFalse(any("testserver" in command for command in commands))
        self.assertFalse(any("wss://" in command for command in commands))
        self.assertTrue(any("install" in command and "--token-file" in command
                            for command in commands))
        self.assertEqual(FakeSSHRunner.upload_calls[0][0],
                         "/home/fake/.tmuxweb-agent/agent.py")
        self.assertEqual(FakeSSHRunner.upload_calls[0][1],
                         (AGENT_DIR / "agent.py").read_bytes())
        self.assertEqual(self.store.get_host(self.host_id)["transport"], "agent")

    def test_install_failure_leaves_the_transport_alone(self):
        class FailingSSHRunner:
            def __init__(self, host):
                self.host = host

            def run(self, remote_command, *, input_bytes=None, timeout=15,
                    resolve_tmux=True):
                return subprocess.CompletedProcess(
                    args=remote_command, returncode=0, stdout=b"/home/fake", stderr=b"")

            def upload_file(self, path, data):
                raise RuntimeError("disk full")

        module = sys.modules["tmuxweb_plugin_agent"]
        original_factory = module._ssh_runner_factory
        try:
            module._ssh_runner_factory = FailingSSHRunner
            response = self.client.post("/api/hosts/%s/agent-install" % self.host2["id"])
        finally:
            module._ssh_runner_factory = original_factory
        self.assertEqual(response.status_code, 503, response.text)
        # the host stays on ssh: a failed install never flips the transport
        self.assertEqual(self.store.get_host(self.host2["id"])["transport"], "ssh")

    # -- tailnet-only policy ---------------------------------------------------
    def _plugin_module(self):
        return sys.modules["tmuxweb_plugin_agent"]

    def test_peer_allowed_matrix(self):
        module = self._plugin_module()
        allowed = [
            "100.123.123.5", "100.64.0.1", "100.127.255.255",  # tailnet CIDR
            "127.0.0.1", "127.9.8.7", "::1",                   # loopback
            "::ffff:100.123.123.5",                             # mapped tailnet
        ]
        for host in allowed:
            self.assertTrue(module._peer_allowed(host), host)
        denied = [
            "8.8.8.8", "1.1.1.1",                               # public
            "192.168.1.5", "10.0.0.8", "172.16.0.1",            # private LAN, not tailnet
            "100.128.0.1",                                      # just outside 100.64/10
            "::ffff:8.8.8.8",                                   # mapped public
            None, "", "testclient", "tmux.122990.xyz",           # unparseable
        ]
        for host in denied:
            self.assertFalse(module._peer_allowed(host), host)

    def test_peer_allowed_custom_cidr_and_invalid_cidr(self):
        module = self._plugin_module()
        self.assertTrue(module._peer_allowed("10.0.0.8", "10.0.0.0/8"))
        self.assertFalse(module._peer_allowed("100.123.123.5", "10.0.0.0/8"))
        # junk CIDR falls back to the Tailscale default instead of denying all
        self.assertTrue(module._peer_allowed("100.123.123.5", "not-a-cidr"))
        self.assertFalse(module._peer_allowed("8.8.8.8", "not-a-cidr"))

    def test_tailscale_ipv4_override_and_discovery(self):
        module = self._plugin_module()
        self.assertEqual(module._tailscale_ipv4("100.123.123.5"), "100.123.123.5")
        with self.assertRaises(ValueError):
            module._tailscale_ipv4("192.168.1.5")     # LAN, not tailnet
        with self.assertRaises(ValueError):
            module._tailscale_ipv4("not-an-ip")
        with self.assertRaises(ValueError):
            module._tailscale_ipv4("8.8.8.8")         # public, not tailnet
        with mock.patch.object(module.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["tailscale", "ip", "-4"], returncode=0,
                stdout="100.123.123.9\n", stderr="")
            self.assertEqual(module._tailscale_ipv4(), "100.123.123.9")
        with mock.patch.object(module.subprocess, "run",
                               side_effect=FileNotFoundError("no tailscale")):
            self.assertIsNone(module._tailscale_ipv4())

    def test_token_response_carries_tailnet_urls(self):
        response = self.client.post("/api/hosts/%s/agent-token" % self.host_id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["server"], "ws://100.123.123.5:8787/ws/agent")
        self.assertEqual(body["program_url"],
                         "http://100.123.123.5:8787/agent/agent.py")
        self.assertNotIn("testserver", response.text)
        self.assertNotIn("wss://", response.text)

    def test_token_issue_fails_without_tailnet_and_keeps_old_token(self):
        module = self._plugin_module()
        old_token = self._issue_token()
        with mock.patch.object(module, "_tailscale_ipv4", return_value=None):
            response = self.client.post("/api/hosts/%s/agent-token" % self.host_id)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("Tailscale", response.json()["detail"])
        # the failed issue did not rotate the old token away
        agent = self._connect_agent(token=old_token)
        self.assertEqual(agent.welcome["host_id"], self.host_id)

    def test_install_fails_without_tailnet(self):
        module = self._plugin_module()
        with mock.patch.object(module, "_tailscale_ipv4", return_value=None):
            with mock.patch.object(
                    module, "_ssh_runner_factory",
                    side_effect=AssertionError("install must fail before SSH")):
                response = self.client.post("/api/hosts/%s/agent-install" % self.host_id)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("Tailscale", response.json()["detail"])
        self.assertEqual(self.store.get_host(self.host_id)["transport"], "agent")

    def test_non_tailnet_peer_gets_4403(self):
        # TestClient's peer is "testclient", not an IP: unparseable, denied.
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/ws/agent"):
                pass
        self.assertEqual(caught.exception.code, 4403)

    # -- host endpoints and the public program -----------------------------------
    def test_hosts_endpoints_carry_the_transport_field(self):
        payload = {"name": "plain", "hostname": "10.0.0.20", "port": 22,
                   "username": "u", "auth_type": "agent", "credential": "",
                   "host_key": "", "fingerprint": "", "verify": False}
        created = self.client.post("/api/remote-hosts", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["transport"], "ssh")     # omitted means ssh
        host_id = created.json()["id"]
        bad = self.client.post("/api/remote-hosts",
                               json={**payload, "transport": "pigeon"})
        self.assertEqual(bad.status_code, 422)
        updated = self.client.put("/api/remote-hosts/%s" % host_id,
                                  json={"transport": "agent", "verify": False})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["transport"], "agent")
        bad_put = self.client.put("/api/remote-hosts/%s" % host_id,
                                  json={"transport": "pigeon"})
        self.assertEqual(bad_put.status_code, 422)
        rows = {row["id"]: row
                for row in self.client.get("/api/remote-hosts").json()["hosts"]}
        self.assertEqual(rows[host_id]["transport"], "agent")
        self.assertFalse(rows[host_id]["agent_online"])
        self.assertIsNone(rows[host_id]["agent_last_seen"])
        self.assertIsNone(rows[host_id]["last_transport"])
        self.assertIsNone(rows[host_id]["last_transport_at"])

    def test_agent_program_is_public_but_never_cached(self):
        anonymous = TestClient(self.app)
        response = anonymous.get("/agent/agent.py")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertTrue(len(response.text) > 100)
        original = self.plugin.agent_file
        try:
            self.plugin.agent_file = Path(tempfile.mkdtemp()) / "missing.py"
            self.assertEqual(anonymous.get("/agent/agent.py").status_code, 404)
        finally:
            self.plugin.agent_file = original


if __name__ == "__main__":
    unittest.main()
