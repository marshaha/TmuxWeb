"""Tests for the agent program itself (plugins/agent/agent.py).

The program is stdlib-only, so these tests exercise it directly: the
hand-rolled WebSocket framing against a minimal socket server, the exec
op and batch handling, the reconnect backoff, and the install / status /
uninstall commands (with HOME pointed at a temp dir, so nothing on the
real machine is touched).

No network beyond 127.0.0.1 is used, and no real pane is ever touched:
the exec tests only run shell commands like ``echo`` in this sandbox.
"""

import base64
import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "agent"))

import agent as agent_program  # noqa: E402


# ---------------------------------------------------------------------------
# A minimal one-connection WebSocket server, for the client-side tests
# ---------------------------------------------------------------------------

class _MiniConn:
    """One accepted socket, with frame helpers built on the agent's codec."""

    def __init__(self, sock):
        self.sock = sock
        self.sock.settimeout(10.0)
        self.buf = bytearray()

    def read_http_request(self):
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("client closed during handshake")
            raw += chunk
            if len(raw) > 65536:
                raise ValueError("handshake request too large")
        head, rest = raw.split(b"\r\n\r\n", 1)
        self.buf += rest
        return head.decode("iso-8859-1")

    def read_frame(self):
        """One frame (unmasking client frames), or None on clean EOF."""
        while True:
            parsed = agent_program.parse_one_frame(bytes(self.buf))
            if parsed is not None:
                _, opcode, payload, consumed = parsed
                del self.buf[:consumed]
                return opcode, payload
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk

    def send_frame(self, payload: bytes, opcode: int = agent_program.OP_TEXT):
        self.sock.sendall(agent_program.encode_frame(
            payload, opcode=opcode, mask=False))

    def send_text(self, text: str):
        self.send_frame(text.encode("utf-8"))

    def send_close(self, code: int):
        self.send_frame(struct.pack("!H", code), opcode=agent_program.OP_CLOSE)


class _MiniWS:
    """Listens on 127.0.0.1, serves one connection through ``handler``."""

    def __init__(self, handler):
        self.handler = handler
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(1)
        self.sock.settimeout(15.0)
        self.error = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            try:
                self.handler(_MiniConn(conn))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - surfaced to the test
            self.error = exc
        finally:
            self.sock.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(timeout=15)
        if self.error is not None:
            raise self.error
        return False


def _accept_key(request_text: str) -> str:
    for line in request_text.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
            return agent_program._expected_accept(key)
    raise AssertionError("handshake request has no Sec-WebSocket-Key")


def _handshake_101(conn: _MiniConn) -> str:
    """Answer the upgrade with 101; returns the request text."""
    request = conn.read_http_request()
    accept = _accept_key(request)
    conn.sock.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii"))
    return request


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

class FrameTests(unittest.TestCase):
    def test_masked_frame_round_trips_with_a_fixed_key(self):
        payload = b"hello agent"
        framed = agent_program.encode_frame(
            payload, mask=True, mask_key=b"\x01\x02\x03\x04")
        parsed = agent_program.parse_one_frame(framed)
        self.assertIsNotNone(parsed)
        fin, opcode, out, consumed = parsed
        self.assertTrue(fin)
        self.assertEqual(opcode, agent_program.OP_TEXT)
        self.assertEqual(out, payload)
        self.assertEqual(consumed, len(framed))

    def test_unmasked_frame_round_trips(self):
        payload = b"plain"
        framed = agent_program.encode_frame(payload, mask=False)
        _, opcode, out, _ = agent_program.parse_one_frame(framed)
        self.assertEqual(opcode, agent_program.OP_TEXT)
        self.assertEqual(out, payload)

    def test_126_and_127_length_forms(self):
        for size in (200, 70000):
            payload = b"x" * size
            framed = agent_program.encode_frame(payload, mask=False)
            _, _, out, consumed = agent_program.parse_one_frame(framed)
            self.assertEqual(out, payload)
            self.assertEqual(consumed, len(framed))

    def test_fin_flag_and_opcode_survive(self):
        framed = agent_program.encode_frame(b"part", fin=False,
                                            opcode=agent_program.OP_CONTINUATION,
                                            mask=False)
        fin, opcode, out, _ = agent_program.parse_one_frame(framed)
        self.assertFalse(fin)
        self.assertEqual(opcode, agent_program.OP_CONTINUATION)
        self.assertEqual(out, b"part")

    def test_truncated_frame_returns_none(self):
        framed = agent_program.encode_frame(b"hello world", mask=False)
        self.assertIsNone(agent_program.parse_one_frame(framed[:-3]))
        self.assertIsNone(agent_program.parse_one_frame(b"\x81"))
        self.assertIsNone(agent_program.parse_one_frame(b""))

    def test_oversized_length_raises(self):
        bad = b"\x81\xff" + struct.pack("!Q", 1 << 63)
        with self.assertRaises(ValueError):
            agent_program.parse_one_frame(bad)

    def test_bad_mask_key_length_raises(self):
        with self.assertRaises(ValueError):
            agent_program.encode_frame(b"x", mask_key=b"\x01\x02")

    def test_decode_frames_handles_a_concatenated_pair(self):
        first = agent_program.encode_frame(b"one", mask=False)
        second = agent_program.encode_frame(b"two", mask=False)
        frames = agent_program.decode_frames(first + second)
        self.assertEqual([(True, 1, b"one"), (True, 1, b"two")], frames)

    def test_decode_frames_rejects_a_trailing_truncation(self):
        first = agent_program.encode_frame(b"one", mask=False)
        with self.assertRaises(ValueError):
            agent_program.decode_frames(first + b"\x81\x05hel")


# ---------------------------------------------------------------------------
# WSConnection against the minimal server
# ---------------------------------------------------------------------------

class WSConnectionTests(unittest.TestCase):
    def test_connect_read_and_send_text(self):
        seen = {}

        def handler(conn):
            request = _handshake_101(conn)
            seen["upgrade"] = "Upgrade: websocket" in request
            opcode, payload = conn.read_frame()
            seen["hello"] = json.loads(payload.decode("utf-8"))
            conn.send_text(json.dumps({"type": "welcome", "host_id": "h1"}))

        with _MiniWS(handler) as server:
            ws = agent_program.WSConnection.connect(
                f"ws://127.0.0.1:{server.port}/ws/agent")
            try:
                ws.send_text(json.dumps({"type": "hello", "token": "t"}))
                self.assertEqual(
                    json.loads(ws.read_message()),
                    {"type": "welcome", "host_id": "h1"})
            finally:
                ws.close()
        self.assertTrue(seen["upgrade"])
        self.assertEqual(seen["hello"]["type"], "hello")

    def test_ping_is_answered_and_the_next_message_is_returned(self):
        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            conn.send_frame(b"ping", opcode=agent_program.OP_PING)
            conn.send_text("after-ping")
            opcode, payload = conn.read_frame()
            self.assertEqual(opcode, agent_program.OP_PONG)
            self.assertEqual(payload, b"ping")

        with _MiniWS(handler) as server:
            ws = agent_program.WSConnection.connect(
                f"ws://127.0.0.1:{server.port}/ws/agent")
            try:
                ws.send_text("{}")
                self.assertEqual(ws.read_message(), "after-ping")
            finally:
                ws.close()

    def test_close_frame_raises_with_its_code(self):
        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            conn.send_close(4403)

        with _MiniWS(handler) as server:
            ws = agent_program.WSConnection.connect(
                f"ws://127.0.0.1:{server.port}/ws/agent")
            try:
                ws.send_text("{}")
                with self.assertRaises(agent_program.ConnectionClosed) as ctx:
                    ws.read_message()
                self.assertEqual(ctx.exception.code, 4403)
            finally:
                ws.close()

    def test_non_101_handshake_is_rejected(self):
        def handler(conn):
            conn.read_http_request()
            conn.sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")

        with _MiniWS(handler) as server:
            with self.assertRaises(agent_program.WebSocketError):
                agent_program.WSConnection.connect(
                    f"ws://127.0.0.1:{server.port}/ws/agent")

    def test_wrong_accept_key_is_rejected(self):
        def handler(conn):
            conn.read_http_request()
            conn.sock.sendall((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: wrong\r\n"
                "\r\n"
            ).encode("ascii"))

        with _MiniWS(handler) as server:
            with self.assertRaises(agent_program.WebSocketError):
                agent_program.WSConnection.connect(
                    f"ws://127.0.0.1:{server.port}/ws/agent")

    def test_unsupported_scheme_and_missing_host(self):
        with self.assertRaises(agent_program.WebSocketError):
            agent_program.WSConnection.connect("http://127.0.0.1:1/agent")
        with self.assertRaises(agent_program.WebSocketError):
            agent_program.WSConnection.connect("ws:///agent")


# ---------------------------------------------------------------------------
# exec / batch
# ---------------------------------------------------------------------------

class ExecTests(unittest.TestCase):
    def setUp(self):
        self.resolver = agent_program.TmuxResolver(override="/nonexistent/tmux")

    def test_echo_returns_stdout_and_zero(self):
        result = agent_program.run_exec(
            {"op": "exec", "command": "echo hello-agent", "timeout": 5},
            self.resolver)
        self.assertEqual(result["ok"], True)
        self.assertEqual(base64.b64decode(result["stdout_b64"]), b"hello-agent\n")
        self.assertEqual(result["stderr_b64"], "")
        self.assertEqual(result["returncode"], 0)

    def test_stderr_and_nonzero_exit_come_back(self):
        result = agent_program.run_exec(
            {"op": "exec", "command": "echo out; echo err >&2; exit 3",
             "timeout": 5}, self.resolver)
        self.assertEqual(result["ok"], True)
        self.assertEqual(base64.b64decode(result["stdout_b64"]), b"out\n")
        self.assertEqual(base64.b64decode(result["stderr_b64"]), b"err\n")
        self.assertEqual(result["returncode"], 3)

    def test_stdin_arrives_through_input_b64(self):
        stdin = base64.b64encode(b"via stdin\n").decode()
        result = agent_program.run_exec(
            {"op": "exec", "command": "cat", "input_b64": stdin, "timeout": 5},
            self.resolver)
        self.assertEqual(result["ok"], True)
        self.assertEqual(base64.b64decode(result["stdout_b64"]), b"via stdin\n")

    def test_bad_ops_are_errors_not_crashes(self):
        for op in ({}, {"op": "exec"}, {"op": "exec", "command": ""},
                   {"op": "exec", "command": "echo x", "input_b64": "!!!"},
                   {"op": "exec", "command": "echo x", "timeout": 0},
                   {"op": "exec", "command": "echo x", "timeout": -1},
                   {"op": "exec", "command": "echo x", "timeout": "soon"}):
            result = agent_program.run_exec(op, self.resolver)
            self.assertEqual(result["ok"], False, op)
            self.assertIn("error", result)

    def test_a_hanging_command_times_out(self):
        started = time.monotonic()
        result = agent_program.run_exec(
            {"op": "exec", "command": "sleep 30", "timeout": 0.3},
            self.resolver)
        self.assertEqual(result["ok"], False)
        self.assertIn("timed out", result["error"])
        self.assertLess(time.monotonic() - started, 15)

    def test_run_op_rejects_unknown_ops(self):
        self.assertEqual(
            agent_program.run_op({"op": "reboot"}, self.resolver)["ok"], False)
        self.assertEqual(
            agent_program.run_op("exec", self.resolver)["ok"], False)
        self.assertEqual(
            agent_program.run_op(None, self.resolver)["ok"], False)

    def test_handle_batch_runs_in_order(self):
        reply = agent_program.handle_batch(
            {"id": "req-1", "ops": [
                {"op": "exec", "command": "echo first", "timeout": 5},
                {"op": "exec", "command": "echo second", "timeout": 5},
                {"op": "unknown"},
            ]}, self.resolver)
        self.assertEqual(reply["id"], "req-1")
        self.assertEqual(reply["ok"], True)
        self.assertEqual(len(reply["results"]), 3)
        self.assertEqual(base64.b64decode(reply["results"][0]["stdout_b64"]),
                         b"first\n")
        self.assertEqual(base64.b64decode(reply["results"][1]["stdout_b64"]),
                         b"second\n")
        self.assertEqual(reply["results"][2]["ok"], False)

    def test_handle_batch_rejects_malformed_requests(self):
        self.assertEqual(
            agent_program.handle_batch({"ops": []}, self.resolver)["ok"], False)
        self.assertEqual(
            agent_program.handle_batch({"id": "x", "ops": "echo hi"},
                                       self.resolver)["ok"], False)
        self.assertEqual(
            agent_program.handle_batch({"id": 7, "ops": []},
                                       self.resolver)["ok"], False)

    def test_handle_batch_runs_slow_ops_in_parallel(self):
        # Four half-second sleeps that would take 2s one after another
        # must finish in about the time of the slowest one, not the sum:
        # the batch runs its ops in a thread pool, not one by one.
        started = time.monotonic()
        reply = agent_program.handle_batch(
            {"id": "par-1", "ops": [
                {"op": "exec", "command": "sleep 0.5; echo one", "timeout": 5},
                {"op": "exec", "command": "sleep 0.5; echo two", "timeout": 5},
                {"op": "exec", "command": "sleep 0.5; echo three", "timeout": 5},
                {"op": "exec", "command": "sleep 0.5; echo four", "timeout": 5},
            ]}, self.resolver)
        elapsed = time.monotonic() - started
        self.assertEqual(reply["ok"], True)
        self.assertEqual(len(reply["results"]), 4)
        # Serial would be 2.0s + spawn overhead; parallel is ~0.5s.
        self.assertLess(elapsed, 1.5)
        outs = [base64.b64decode(r["stdout_b64"]) for r in reply["results"]]
        self.assertEqual(outs, [b"one\n", b"two\n", b"three\n", b"four\n"])

    def test_handle_batch_results_stay_in_order_when_ops_finish_out_of_order(self):
        # The slow op is first and finishes last; its result must still
        # come back first — results are assembled in op order.
        reply = agent_program.handle_batch(
            {"id": "ord-1", "ops": [
                {"op": "exec", "command": "sleep 0.4; echo slow", "timeout": 5},
                {"op": "exec", "command": "echo fast", "timeout": 5},
            ]}, self.resolver)
        self.assertEqual(reply["ok"], True)
        outs = [base64.b64decode(r["stdout_b64"]) for r in reply["results"]]
        self.assertEqual(outs, [b"slow\n", b"fast\n"])

    def test_handle_batch_outputs_do_not_interleave(self):
        # Each op's stdout/stderr belong to that op alone, even when the
        # ops run at the same time.
        reply = agent_program.handle_batch(
            {"id": "mix-1", "ops": [
                {"op": "exec", "command": "printf 'a1\\na2\\na3\\n'", "timeout": 5},
                {"op": "exec", "command": "printf 'b1\\nb2\\nb3\\n'", "timeout": 5},
            ]}, self.resolver)
        outs = [base64.b64decode(r["stdout_b64"]) for r in reply["results"]]
        self.assertEqual(outs, [b"a1\na2\na3\n", b"b1\nb2\nb3\n"])

    def test_handle_message_blocks_until_its_batch_is_done(self):
        # _handle_message itself still blocks until its whole batch —
        # pool included — has finished. (It now runs on a message-pool
        # worker instead of the main loop, so the loop can dispatch the
        # next message meanwhile; but one message's own reply is only
        # sent once its batch is done, and a send sequence still cannot
        # overtake itself on the agent.)
        sent = []

        class _CaptureConn:
            def send_text(self, text):
                sent.append(json.loads(text))

        conn = _CaptureConn()
        started = time.monotonic()
        agent_program._handle_message(
            conn, json.dumps({"id": "m-1", "ops": [
                {"op": "exec", "command": "sleep 0.5; echo first",
                 "timeout": 5}]}),
            self.resolver)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        agent_program._handle_message(
            conn, json.dumps({"id": "m-2", "ops": [
                {"op": "exec", "command": "echo second", "timeout": 5}]}),
            self.resolver)
        self.assertEqual([m["id"] for m in sent], ["m-1", "m-2"])
        self.assertEqual(
            base64.b64decode(sent[0]["results"][0]["stdout_b64"]), b"first\n")
        self.assertEqual(
            base64.b64decode(sent[1]["results"][0]["stdout_b64"]), b"second\n")


class TmuxResolverTests(unittest.TestCase):
    def test_override_is_used_as_is_and_never_reprobed(self):
        resolver = agent_program.TmuxResolver(override="/opt/bin/tmux")
        self.assertTrue(resolver.fixed)
        self.assertEqual(resolver.refresh(), "/opt/bin/tmux")
        self.assertEqual(resolver.path, "/opt/bin/tmux")

    def test_refresh_finds_tmux_on_path_and_empty_when_absent(self):
        # Wherever tmux lives on this machine (never assume the sandbox
        # layout): with only its directory on PATH it must be found.
        found = shutil.which("tmux")
        if found:
            with mock.patch.dict(os.environ, {"PATH": str(Path(found).parent)}):
                self.assertTrue(agent_program.TmuxResolver().refresh())
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-dir"}):
            self.assertEqual(agent_program.TmuxResolver().refresh(), "")

    def test_probe_tmux_returns_a_string(self):
        self.assertIsInstance(agent_program.probe_tmux(), str)


# ---------------------------------------------------------------------------
# Backoff / helpers
# ---------------------------------------------------------------------------

class BackoffTests(unittest.TestCase):
    def test_backoff_doubles_then_caps(self):
        self.assertEqual(agent_program.backoff_delay(0), 1.0)
        self.assertEqual(agent_program.backoff_delay(1), 2.0)
        self.assertEqual(agent_program.backoff_delay(5), 32.0)
        self.assertEqual(agent_program.backoff_delay(6), 60.0)
        self.assertEqual(agent_program.backoff_delay(100), 60.0)
        self.assertEqual(agent_program.backoff_delay(-3), 1.0)

    def test_server_host_parses_urls(self):
        self.assertEqual(
            agent_program.server_host("wss://tmux.example.com/ws/agent"),
            "tmux.example.com")
        self.assertEqual(
            agent_program.server_host("ws://127.0.0.1:8787/ws/agent"),
            "127.0.0.1")
        self.assertEqual(agent_program.server_host("not a url"), "not a url")


# ---------------------------------------------------------------------------
# Config files
# ---------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_write_then_load_round_trips_with_mode_0600(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            agent_program.write_config(path, "wss://h/ws/agent", "sekret", "/usr/bin/tmux")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            data = agent_program.load_config_file(path)
            self.assertEqual(data, {"server": "wss://h/ws/agent",
                                    "token": "sekret", "tmux": "/usr/bin/tmux"})

    def test_load_rejects_bad_json_and_non_objects(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{nope", encoding="utf-8")
            with self.assertRaises(ValueError):
                agent_program.load_config_file(bad)
            arr = Path(tmp) / "arr.json"
            arr.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(ValueError):
                agent_program.load_config_file(arr)

    def test_unit_and_run_sh_contents_name_the_files(self):
        unit = agent_program._unit_content("/home/u/.tmuxweb-agent/agent.py",
                                           "/home/u/.config/tmuxweb-agent/config.json")
        self.assertIn("ExecStart=", unit)
        self.assertIn("agent.py", unit)
        self.assertIn("config.json", unit)
        self.assertIn("Restart=on-failure", unit)
        script = agent_program._run_sh_content(
            "/home/u/.tmuxweb-agent/agent.py",
            "/home/u/.config/tmuxweb-agent/config.json")
        self.assertIn("while true", script)
        self.assertIn("agent.py", script)
        self.assertIn("config.json", script)


# ---------------------------------------------------------------------------
# install / status / uninstall (HOME pointed at a temp dir)
# ---------------------------------------------------------------------------

class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._patcher.start()
        # No systemd user bus in the sandbox: a real probe runs
        # `systemctl --user daemon-reload` and burns its 15s timeout
        # before failing, so pin the answer — the nohup path below is
        # what these tests exercise. The probe logic itself is covered
        # by SystemdProbeTests with mocked helpers.
        self._probe = mock.patch.object(
            agent_program, "_systemd_user_available", return_value=False)
        self._probe.start()
        self.addCleanup(self._stop_agent)

    def tearDown(self):
        self._probe.stop()
        self._patcher.stop()
        self._tmp.cleanup()

    def _stop_agent(self):
        try:
            agent_program.cmd_uninstall(
                agent_program.build_parser().parse_args(["uninstall"]))
        except Exception:
            pass

    def _args(self, argv):
        return agent_program.build_parser().parse_args(argv)

    def test_install_needs_a_server_and_a_token(self):
        self.assertEqual(
            agent_program.cmd_install(self._args(["install"])), 2)
        self.assertEqual(
            agent_program.cmd_install(
                self._args(["install", "--server", "ws://127.0.0.1:9/agent"])), 2)
        self.assertEqual(
            agent_program.cmd_install(
                self._args(["install", "--server", "ws://127.0.0.1:9/agent",
                            "--token-file", str(self.home / "no-such-token")])), 2)

    def test_install_status_uninstall_round_trip_over_nohup(self):
        token_path = self.home / "token.txt"
        token_path.write_text("test-token-abc", encoding="utf-8")
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_program.cmd_install(self._args([
                "install", "--server", "ws://127.0.0.1:9/ws/agent",
                "--token-file", str(token_path)]))
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        # The token never appears on stdout/stderr.
        self.assertNotIn("test-token-abc", out.getvalue())
        self.assertNotIn("test-token-abc", err.getvalue())
        config = agent_program.load_config_file(
            self.home / ".config" / "tmuxweb-agent" / "config.json")
        self.assertEqual(config["server"], "ws://127.0.0.1:9/ws/agent")
        self.assertEqual(config["token"], "test-token-abc")
        self.assertEqual(
            stat.S_IMODE((self.home / ".config" / "tmuxweb-agent"
                          / "config.json").stat().st_mode), 0o600)
        self.assertTrue((self.home / ".config" / "tmuxweb-agent"
                         / "run.sh").stat().st_mode & 0o111)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_program.cmd_status(self._args(["status"]))
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn("running", out.getvalue())
        self.assertIn("ws://127.0.0.1:9/ws/agent", out.getvalue())

        # Re-install is idempotent: same files, still running, one loop.
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_program.cmd_install(self._args([
                "install", "--server", "ws://127.0.0.1:9/ws/agent",
                "--token-file", str(token_path)]))
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())

        with redirect_stdout(out), redirect_stderr(err):
            code = agent_program.cmd_uninstall(self._args(["uninstall"]))
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertFalse((self.home / ".config" / "tmuxweb-agent"
                          / "config.json").exists())
        self.assertFalse((self.home / ".config" / "tmuxweb-agent"
                          / "run.sh").exists())

        with redirect_stdout(out), redirect_stderr(err):
            code = agent_program.cmd_status(self._args(["status"]))
        self.assertEqual(code, 1)
        self.assertIn("not running", out.getvalue())

    def test_uninstall_when_nothing_is_installed(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = agent_program.cmd_uninstall(self._args(["uninstall"]))
        self.assertEqual(code, 0)
        self.assertIn("not installed", out.getvalue())


# ---------------------------------------------------------------------------
# systemd probe logic (helpers mocked: the real probe needs a user bus)
# ---------------------------------------------------------------------------

class SystemdProbeTests(unittest.TestCase):
    def _ok(self, returncode=0):
        return mock.Mock(returncode=returncode)

    def test_no_systemctl_binary_means_unavailable(self):
        with mock.patch.object(agent_program.shutil, "which", return_value=None):
            self.assertFalse(agent_program._systemd_user_available())

    def test_daemon_reload_zero_means_available(self):
        with mock.patch.object(agent_program.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(agent_program, "_run_quiet",
                                  return_value=self._ok(0)):
            self.assertTrue(agent_program._systemd_user_available())

    def test_daemon_reload_failing_or_erroring_means_unavailable(self):
        with mock.patch.object(agent_program.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(agent_program, "_run_quiet",
                                  return_value=self._ok(1)):
            self.assertFalse(agent_program._systemd_user_available())
        with mock.patch.object(agent_program.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(agent_program, "_run_quiet", return_value=None):
            self.assertFalse(agent_program._systemd_user_available())


# ---------------------------------------------------------------------------
# run_daemon against the minimal server
# ---------------------------------------------------------------------------

class DaemonTests(unittest.TestCase):
    def test_auth_rejection_stops_the_daemon_with_code_3(self):
        def handler(conn):
            _handshake_101(conn)
            opcode, payload = conn.read_frame()
            hello = json.loads(payload.decode("utf-8"))
            self.assertEqual(hello["type"], "hello")
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        with _MiniWS(handler) as server:
            code = agent_program.run_daemon(
                f"ws://127.0.0.1:{server.port}/ws/agent", "bad-token")
        self.assertEqual(code, 3)

    def test_exec_round_trip_through_the_daemon(self):
        seen = {}

        def handler(conn):
            _handshake_101(conn)
            opcode, payload = conn.read_frame()
            seen["hello"] = json.loads(payload.decode("utf-8"))
            conn.send_text(json.dumps({
                "id": "req-1",
                "ops": [{"op": "exec", "command": "echo hello-agent",
                         "timeout": 5}]}))
            opcode, payload = conn.read_frame()
            seen["reply"] = json.loads(payload.decode("utf-8"))
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        with _MiniWS(handler) as server:
            code = agent_program.run_daemon(
                f"ws://127.0.0.1:{server.port}/ws/agent", "good-token")
        self.assertEqual(code, 3)
        self.assertEqual(seen["hello"]["type"], "hello")
        self.assertEqual(seen["hello"]["token"], "good-token")
        reply = seen["reply"]
        self.assertEqual(reply["id"], "req-1")
        self.assertEqual(reply["ok"], True)
        self.assertEqual(
            base64.b64decode(reply["results"][0]["stdout_b64"]), b"hello-agent\n")
        self.assertEqual(reply["results"][0]["returncode"], 0)

    def test_connect_failure_then_stop_returns_zero(self):
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        code = agent_program.run_daemon(
            "ws://127.0.0.1:1/ws/agent", "any-token", stop_event=stop)
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# Message-level concurrency: the bounded pool _serve_connection dispatches to
# ---------------------------------------------------------------------------

class MessagePoolTests(unittest.TestCase):
    """The pool itself: bounded workers, daemon threads, clean shutdown."""

    def _pool_threads(self):
        return [t for t in threading.enumerate()
                if t.name.startswith("tmuxweb-agent-msg")]

    def _wait_for_no_pool_threads(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        remaining = self._pool_threads()
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            remaining = self._pool_threads()
        return remaining

    def test_pool_runs_at_most_max_workers(self):
        # Ten slow tasks on an 8-worker pool: at most 8 message workers
        # ever exist, and all ten tasks still complete.
        done = []
        pool = agent_program._MessagePool(
            agent_program._MESSAGE_MAX_WORKERS,
            lambda item: (time.sleep(1), done.append(item)))
        try:
            for index in range(10):
                self.assertTrue(pool.submit(index))
            time.sleep(0.3)
            self.assertEqual(len(self._pool_threads()),
                             agent_program._MESSAGE_MAX_WORKERS)
            deadline = time.monotonic() + 5
            while len(done) < 10 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(sorted(done), list(range(10)))
        finally:
            pool.shutdown()
        self.assertEqual(self._wait_for_no_pool_threads(), [])

    def test_a_failing_task_does_not_kill_the_pool(self):
        done = []

        def handler(item):
            if item == "boom":
                raise RuntimeError("boom")
            done.append(item)

        pool = agent_program._MessagePool(2, handler)
        try:
            pool.submit("boom")
            pool.submit("ok")
            deadline = time.monotonic() + 5
            while "ok" not in done and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(done, ["ok"])
            self.assertEqual(len(self._pool_threads()), 2)
        finally:
            pool.shutdown()
        self.assertEqual(self._wait_for_no_pool_threads(), [])

    def test_shutdown_drops_queued_work(self):
        # Two workers, ten short tasks, shutdown at once: only the tasks
        # that already started may finish; the queued ones are dropped
        # and never run. (Broken shutdown would run all ten.)
        done = []

        def handler(item):
            time.sleep(0.2)
            done.append(item)

        pool = agent_program._MessagePool(2, handler)
        try:
            for index in range(10):
                pool.submit(index)
            pool.shutdown()
            time.sleep(3)
            self.assertLess(len(done), 10)
        finally:
            pool.shutdown()
        self.assertEqual(self._wait_for_no_pool_threads(), [])


class ConcurrentDaemonTests(unittest.TestCase):
    """The daemon serves messages concurrently (see _serve_connection)."""

    def _run_daemon(self, handler):
        with _MiniWS(handler) as server:
            return agent_program.run_daemon(
                f"ws://127.0.0.1:{server.port}/ws/agent", "good-token")

    def test_fast_message_returns_while_slow_message_is_in_flight(self):
        # The core acceptance: a 5s slow batch must not hold up a fast
        # one behind it. On the old one-message-at-a-time loop the fast
        # reply only arrives after ~5s.
        arrivals = {}

        def handler(conn):
            _handshake_101(conn)
            opcode, payload = conn.read_frame()
            self.assertEqual(json.loads(payload.decode("utf-8"))["type"],
                             "hello")
            sent_at = time.monotonic()
            conn.send_text(json.dumps({"id": "slow-1", "ops": [
                {"op": "exec", "command": "sleep 5; echo slow",
                 "timeout": 10}]}))
            conn.send_text(json.dumps({"id": "fast-1", "ops": [
                {"op": "exec", "command": "echo fast", "timeout": 5}]}))
            deadline = sent_at + 10
            while len(arrivals) < 2 and time.monotonic() < deadline:
                frame = conn.read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode != agent_program.OP_TEXT:
                    continue
                msg = json.loads(payload.decode("utf-8"))
                if "id" in msg:
                    arrivals[msg["id"]] = time.monotonic() - sent_at
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        code = self._run_daemon(handler)
        self.assertEqual(code, 3)
        self.assertIn("fast-1", arrivals)
        self.assertIn("slow-1", arrivals)
        self.assertLess(arrivals["fast-1"], 2.0)
        self.assertGreaterEqual(arrivals["slow-1"], 4.5)

    def test_concurrent_replies_are_matched_by_request_id(self):
        # Five batches with different sleeps and distinct outputs, sent
        # back-to-back: each reply must carry its own id and its own
        # output, even though they finish out of order.
        jobs = [("j1", 0.5, "one"), ("j2", 0.1, "two"), ("j3", 0.3, "three"),
                ("j4", 0.0, "four"), ("j5", 0.2, "five")]
        replies = {}

        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            for job_id, delay, word in jobs:
                conn.send_text(json.dumps({"id": job_id, "ops": [
                    {"op": "exec",
                     "command": f"sleep {delay}; echo {word}",
                     "timeout": 5}]}))
            deadline = time.monotonic() + 10
            while len(replies) < len(jobs) and time.monotonic() < deadline:
                frame = conn.read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode != agent_program.OP_TEXT:
                    continue
                msg = json.loads(payload.decode("utf-8"))
                if "id" in msg:
                    replies[msg["id"]] = msg
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        code = self._run_daemon(handler)
        self.assertEqual(code, 3)
        self.assertEqual(set(replies), {job_id for job_id, _, _ in jobs})
        for job_id, _, word in jobs:
            out = base64.b64decode(
                replies[job_id]["results"][0]["stdout_b64"])
            self.assertEqual(out, (word + "\n").encode())

    def test_heartbeat_continues_while_a_message_is_in_flight(self):
        # While a 3s batch runs, the main loop must still emit
        # heartbeats on schedule instead of blocking behind the batch.
        # The interval is patched down so the test can observe it.
        heartbeats = []
        old_interval = agent_program.HEARTBEAT_INTERVAL
        agent_program.HEARTBEAT_INTERVAL = 0.5
        try:
            def handler(conn):
                _handshake_101(conn)
                conn.read_frame()  # hello
                conn.send_text(json.dumps({"id": "slow-hb", "ops": [
                    {"op": "exec", "command": "sleep 3; echo slow",
                     "timeout": 5}]}))
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline:
                    frame = conn.read_frame()
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode != agent_program.OP_TEXT:
                        continue
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("type") == "heartbeat":
                        heartbeats.append(time.monotonic())
                conn.send_close(agent_program.CLOSE_AUTH_FAILED)

            code = self._run_daemon(handler)
            self.assertEqual(code, 3)
            self.assertGreaterEqual(len(heartbeats), 1)
        finally:
            agent_program.HEARTBEAT_INTERVAL = old_interval

    def test_send_sequence_order_is_preserved(self):
        # A dependent chain sent one op at a time (each after the
        # previous reply, as the server's registry.call does) executes
        # in order even with a slow message running alongside it.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = os.path.join(tmp, "chain.txt")
        seen = {}

        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            conn.send_text(json.dumps({"id": "slow-chain", "ops": [
                {"op": "exec", "command": "sleep 1; echo slow",
                 "timeout": 5}]}))
            for step, word in (("c1", "one"), ("c2", "two")):
                conn.send_text(json.dumps({"id": step, "ops": [
                    {"op": "exec",
                     "command": f"printf '%s\\n' {word} >> '{marker}'",
                     "timeout": 5}]}))
                while step not in seen:
                    frame = conn.read_frame()
                    if frame is None:
                        raise AssertionError("connection closed mid-chain")
                    opcode, payload = frame
                    if opcode != agent_program.OP_TEXT:
                        continue
                    msg = json.loads(payload.decode("utf-8"))
                    if "id" in msg:
                        seen[msg["id"]] = msg
            conn.send_text(json.dumps({"id": "c3", "ops": [
                {"op": "exec", "command": f"cat '{marker}'",
                 "timeout": 5}]}))
            while "c3" not in seen:
                frame = conn.read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode != agent_program.OP_TEXT:
                    continue
                msg = json.loads(payload.decode("utf-8"))
                if "id" in msg:
                    seen[msg["id"]] = msg
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        code = self._run_daemon(handler)
        self.assertEqual(code, 3)
        out = base64.b64decode(seen["c3"]["results"][0]["stdout_b64"])
        self.assertEqual(out, b"one\ntwo\n")

    def test_bad_message_does_not_break_later_messages(self):
        replies = {}

        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            conn.send_text(json.dumps({"id": "bad-1", "ops": "not-a-list"}))
            conn.send_text(json.dumps({"id": "good-1", "ops": [
                {"op": "exec", "command": "echo good", "timeout": 5}]}))
            deadline = time.monotonic() + 10
            while len(replies) < 2 and time.monotonic() < deadline:
                frame = conn.read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode != agent_program.OP_TEXT:
                    continue
                msg = json.loads(payload.decode("utf-8"))
                if "id" in msg:
                    replies[msg["id"]] = msg
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        code = self._run_daemon(handler)
        self.assertEqual(code, 3)
        self.assertEqual(replies["bad-1"]["ok"], False)
        self.assertEqual(replies["good-1"]["ok"], True)
        out = base64.b64decode(
            replies["good-1"]["results"][0]["stdout_b64"])
        self.assertEqual(out, b"good\n")

    def test_daemon_returns_promptly_on_stop_with_a_message_in_flight(self):
        # Stop mid-batch: the main loop must notice at once instead of
        # waiting out the 5s op. (The worker itself is a daemon thread
        # and keeps running its op; it just cannot hold up the return.)
        stop = threading.Event()

        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            conn.send_text(json.dumps({"id": "slow-stop", "ops": [
                {"op": "exec", "command": "sleep 5; echo slow",
                 "timeout": 10}]}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                frame = conn.read_frame()
                if frame is None:
                    return

        with _MiniWS(handler) as server:
            threading.Timer(0.5, stop.set).start()
            started = time.monotonic()
            code = agent_program.run_daemon(
                f"ws://127.0.0.1:{server.port}/ws/agent", "good-token",
                stop_event=stop)
            elapsed = time.monotonic() - started
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2.0)

    def test_disconnect_leaves_no_pool_threads(self):
        # The server closes mid-batch: the daemon returns at once, and
        # no message-pool thread lingers past the in-flight op's time.
        # The handler keeps the socket open a while after the close
        # frame: on the old serial loop the in-flight reply must still
        # be deliverable, or the send fails and the daemon reconnects
        # forever instead of returning.
        def handler(conn):
            _handshake_101(conn)
            conn.read_frame()  # hello
            conn.send_text(json.dumps({"id": "slow-disc", "ops": [
                {"op": "exec", "command": "sleep 3; echo slow",
                 "timeout": 5}]}))
            time.sleep(0.2)  # let the agent pick the message up
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)
            time.sleep(5)  # keep the socket open for the in-flight reply

        with _MiniWS(handler) as server:
            started = time.monotonic()
            code = agent_program.run_daemon(
                f"ws://127.0.0.1:{server.port}/ws/agent", "good-token")
            elapsed = time.monotonic() - started
        self.assertEqual(code, 3)
        self.assertLess(elapsed, 2.0)
        deadline = time.monotonic() + 5
        remaining = [t for t in threading.enumerate()
                     if t.name.startswith("tmuxweb-agent-msg")]
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            remaining = [t for t in threading.enumerate()
                         if t.name.startswith("tmuxweb-agent-msg")]
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
