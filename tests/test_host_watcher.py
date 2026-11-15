"""Host-side "needs you" watcher (stacks on PR #24).

Covers, in the sandbox only, with a temp HOME and scratch tmux
sessions — never a real pane or host:

* hook event parsing (tolerant field names) and per-pane state
  transitions: a permission event sets waiting, a result/stop event
  clears it, stale events expire;
* the ported session-file decision point (Claude / Codex / Kimi);
* control-mode %output parsing and screen-quiet detection, including
  a live scratch tmux server over a scratch socket;
* the ``needs_input`` op end to end (handle_batch + the daemon);
* the server-side merge: agent event wins, agent silence falls back
  to the PR #24 gate, old agents degrade to {}.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "agent"))

import agent as agent_program  # noqa: E402
import tmux_web  # noqa: E402
import transport as agent_transport  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO / "plugins" / "agent" / "hooks" / "tmuxweb-hook.py"


def _claude_assistant(tool_id="toolu_1"):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": tool_id,
                                     "name": "Bash", "input": {}}]}}


def _write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    return path


class _TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.resolver = agent_program.TmuxResolver(override="/usr/bin/tmux")

    def watcher(self, **kwargs):
        return agent_program.HostWatcher(self.resolver, home=self.home, **kwargs)

    def events_file(self, lines):
        path = self.home / ".config" / "tmuxweb-agent" / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines),
                        encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Hook event parsing
# ---------------------------------------------------------------------------

class ParseHookEventTests(unittest.TestCase):
    def test_kimi_payload_fields(self):
        """Verified against the official Kimi hooks docs page."""
        event = agent_program.parse_hook_event({
            "hook_event_name": "PermissionRequest",
            "session_id": "session_abc", "session_title": "Fix the login page",
            "client_type": "kimi_code_cli", "cwd": "/path/to/project"})
        self.assertEqual(event["waiting"], True)
        self.assertEqual(event["session_id"], "session_abc")
        self.assertEqual(event["cwd"], "/path/to/project")

    def test_claude_notification_type(self):
        """Claude Notification carries notification_type, not the event."""
        event = agent_program.parse_hook_event({
            "hook_event_name": "Notification", "notification_type": "permission_prompt",
            "session_id": "abc123", "cwd": "/proj",
            "transcript_path": "/home/u/.claude/projects/-proj/abc123.jsonl"})
        self.assertEqual(event["waiting"], True)
        self.assertEqual(event["session_id"], "abc123")

    def test_claude_idle_prompt_is_not_waiting(self):
        event = agent_program.parse_hook_event({
            "hook_event_name": "Notification", "notification_type": "idle_prompt",
            "session_id": "abc123", "cwd": "/proj"})
        self.assertIsNone(event)  # idle_prompt is "done", not "needs you"

    def test_session_id_aliases_and_transcript_fallback(self):
        by_alias = agent_program.parse_hook_event({
            "hook_event_name": "PermissionRequest", "sessionId": "s-9",
            "cwd": "/proj"})
        self.assertEqual(by_alias["session_id"], "s-9")
        by_transcript = agent_program.parse_hook_event({
            "hook_event_name": "Stop",
            "transcript_path": "/home/u/.codex/sessions/2026/rollout-xyz.jsonl"})
        self.assertEqual(by_transcript["session_id"], "rollout-xyz")
        self.assertEqual(by_transcript["waiting"], False)

    def test_unknown_events_are_ignored(self):
        for payload in ({"hook_event_name": "PreToolUse", "session_id": "s"},
                        {"hook_event_name": "SessionStart", "session_id": "s"},
                        {"hook_event_name": "Notification",
                         "notification_type": "auth_success"},
                        "not a dict", None, {}):
            self.assertIsNone(agent_program.parse_hook_event(payload), payload)


# ---------------------------------------------------------------------------
# Hook state transitions
# ---------------------------------------------------------------------------

class HookStateTests(_TempHome):
    def test_permission_event_sets_waiting_by_session_id(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        watcher = self.watcher()
        out = watcher.snapshot([{"pane_id": "%0", "provider": "claude",
                                 "cwd": "/proj", "session_id": "sess-1"}],
                               now=now)
        self.assertTrue(out["%0"]["waiting"])
        self.assertEqual(out["%0"]["source"], "hook")
        self.assertEqual(out["%0"]["since"], now)

    def test_permission_result_clears_it(self):
        now = time.time()
        self.events_file([
            {"hook_event_name": "PermissionRequest", "session_id": "sess-1",
             "cwd": "/proj", "timestamp": now - 60},
            {"hook_event_name": "PermissionResult", "session_id": "sess-1",
             "cwd": "/proj", "timestamp": now - 10},
        ])
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "codex", "cwd": "/proj",
              "session_id": "sess-1"}], now=now)
        self.assertFalse(out["%0"]["waiting"])
        self.assertIsNone(out["%0"]["source"])

    def test_stop_clears_it_and_a_later_prompt_re_sets_it(self):
        now = time.time()
        self.events_file([
            {"hook_event_name": "PermissionRequest", "session_id": "sess-1",
             "cwd": "/proj", "timestamp": now - 120},
            {"hook_event_name": "Stop", "session_id": "sess-1",
             "cwd": "/proj", "timestamp": now - 60},
            {"hook_event_name": "PermissionRequest", "session_id": "sess-1",
             "cwd": "/proj", "timestamp": now - 5},
        ])
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "kimi", "cwd": "/proj",
              "session_id": "sess-1"}], now=now)
        self.assertTrue(out["%0"]["waiting"])
        self.assertEqual(out["%0"]["since"], now - 5)

    def test_cwd_match_works_when_the_session_id_is_unknown(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "", "cwd": "/proj",
                           "timestamp": now}])
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}],
            now=now)
        self.assertTrue(out["%0"]["waiting"])

    def test_events_for_another_session_do_not_light_this_pane(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "other", "cwd": "/elsewhere",
                           "timestamp": now}])
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
              "session_id": "sess-1"}], now=now)
        self.assertFalse(out["%0"]["waiting"])

    def test_stale_events_expire(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now - 7200}])
        out = self.watcher(event_ttl=3600).snapshot(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
              "session_id": "sess-1"}], now=now)
        self.assertFalse(out["%0"]["waiting"])

    def test_a_malformed_line_does_not_break_the_tail(self):
        now = time.time()
        path = self.events_file([{"hook_event_name": "PermissionRequest",
                                  "session_id": "sess-1", "cwd": "/proj",
                                  "timestamp": now}])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
              "session_id": "sess-1"}], now=now)
        self.assertTrue(out["%0"]["waiting"])

    def test_the_event_file_is_rotated_when_it_grows_past_the_cap(self):
        path = self.events_file([])
        path.write_text("".join("x" * 100 + "\n" for _ in range(3000)),
                        encoding="utf-8")
        self.assertGreater(path.stat().st_size, 256 * 1024)
        watcher = self.watcher()
        watcher.snapshot([{"pane_id": "%0", "provider": "claude",
                           "cwd": "/proj"}], now=time.time())
        self.assertLess(path.stat().st_size, 256 * 1024)


# ---------------------------------------------------------------------------
# Ported session-file decision point
# ---------------------------------------------------------------------------

class SessionFileDecisionTests(_TempHome):
    def _claude_file(self, cwd="/proj", records=(), name="sess-1"):
        project = self.home / ".claude" / "projects" / "-proj"
        return _write_jsonl(project / (name + ".jsonl"), list(records))

    def test_claude_outstanding_call_is_a_decision_point(self):
        path = self._claude_file(records=[{"type": "user"},
                                          _claude_assistant("toolu_1")])
        self.assertTrue(agent_program._watcher_decision_point("claude", path))

    def test_claude_turn_end_is_a_decision_point(self):
        path = self._claude_file(records=[
            {"type": "user"}, {"type": "system", "subtype": "turn_duration"}])
        self.assertTrue(agent_program._watcher_decision_point("claude", path))

    def test_claude_result_returned_is_not(self):
        path = self._claude_file(records=[
            {"type": "user"}, _claude_assistant("toolu_1"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "ok"}]}}])
        self.assertFalse(agent_program._watcher_decision_point("claude", path))

    def test_codex_outstanding_call_is_a_decision_point(self):
        path = self.home / ".codex" / "sessions" / "2026" / "rollout-1.jsonl"
        _write_jsonl(path, [
            {"type": "session_meta",
             "payload": {"cwd": "/proj", "id": "thread-1"}},
            {"type": "response_item", "payload": {
                "type": "function_call", "call_id": "call_1"}}])
        self.assertTrue(agent_program._watcher_decision_point("codex", path))

    def test_kimi_tool_call_is_a_decision_point_until_step_end(self):
        state_dir = (self.home / ".kimi-code" / "sessions" / "wd_abc"
                     / "session_xyz")
        wire = state_dir / "agents" / "main" / "wire.jsonl"
        (state_dir).mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(json.dumps(
            {"cwd": "/proj", "updatedAt": 1780000000000}), encoding="utf-8")
        _write_jsonl(wire, [{"type": "llm.request"},
                            {"type": "tool.call"}])
        self.assertTrue(agent_program._watcher_decision_point("kimi", wire))
        _write_jsonl(wire, [{"type": "llm.request"}, {"type": "tool.call"},
                      {"type": "step.end"}])
        self.assertFalse(agent_program._watcher_decision_point("kimi", wire))

    def test_hermes_never_has_a_decision_point(self):
        path = self._claude_file(records=[{"type": "user"},
                                          _claude_assistant("toolu_1")])
        self.assertFalse(agent_program._watcher_decision_point("hermes", path))

    def test_resolution_finds_the_newest_claude_session_for_a_cwd(self):
        older = self._claude_file(records=[{"type": "user"}], name="old")
        newer = self._claude_file(records=[{"type": "user"},
                                           _claude_assistant()], name="new")
        os.utime(older, (1000000000, 1000000000))
        os.utime(newer, (1700000000, 1700000000))
        found = agent_program._watcher_resolve_session_file(
            "claude", "/proj", home=self.home)
        self.assertEqual(found, newer)


# ---------------------------------------------------------------------------
# File + control-mode composition
# ---------------------------------------------------------------------------

class CompositionTests(_TempHome):
    def _claude_waiting_file(self):
        return self._claude_file(records=[{"type": "user"},
                                          _claude_assistant()])

    def _claude_file(self, records):
        project = self.home / ".claude" / "projects" / "-proj"
        return _write_jsonl(project / "sess-1.jsonl", list(records))

    def test_decision_point_alone_without_control_mode_does_not_wait(self):
        """The watcher must not duplicate the server gate's false
        positive: a decision point with no quiet signal is not waiting."""
        self._claude_waiting_file()
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
              "session_id": "sess-1"}])
        self.assertFalse(out["%0"]["waiting"])
        self.assertTrue(out["%0"]["decision_point"])
        self.assertFalse(out["%0"]["control_quiet"])

    def test_decision_point_plus_quiet_screen_waits(self):
        self._claude_waiting_file()
        watcher = self.watcher()
        with mock.patch.object(watcher, "_control_quiet", return_value=True):
            out = watcher.snapshot(
                [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
                  "session_id": "sess-1"}])
        self.assertTrue(out["%0"]["waiting"])
        self.assertEqual(out["%0"]["source"], "control-mode")

    def test_hook_wins_even_when_the_file_has_no_decision_point(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        # No session file at all: the hook is exact and stands alone.
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
              "session_id": "sess-1"}], now=now)
        self.assertTrue(out["%0"]["waiting"])
        self.assertEqual(out["%0"]["source"], "hook")
        self.assertFalse(out["%0"]["decision_point"])

    def test_unknown_provider_never_waits(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "", "cwd": "/proj",
                           "timestamp": now}])
        # A hook event with no session id still matches by cwd — but a
        # pane whose provider the watcher cannot judge and whose cwd
        # does not match must stay dark.
        out = self.watcher().snapshot(
            [{"pane_id": "%0", "provider": "hermes", "cwd": "/other"}],
            now=now)
        self.assertFalse(out["%0"]["waiting"])


# ---------------------------------------------------------------------------
# Control-mode parsing (no tmux needed)
# ---------------------------------------------------------------------------

class ControlModeParseTests(unittest.TestCase):
    def test_output_lines_update_last_output_per_pane(self):
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        client.handle_line("%output %0 hello", now=1000.0)
        client.handle_line("%output %1 world", now=1001.0)
        client.handle_line("%begin 1 2 3", now=1002.0)
        client.handle_line("%session-changed $0 scratch", now=1002.0)
        client.handle_line("garbage", now=1002.0)
        self.assertEqual(client.quiet_seconds("%0", now=1005.0), 5.0)
        self.assertEqual(client.quiet_seconds("%1", now=1005.0), 4.0)

    def test_quiet_is_unknown_when_never_attached(self):
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        self.assertIsNone(client.quiet_seconds("%0", now=1005.0))

    def test_a_pane_with_no_output_counts_quiet_since_attach(self):
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        client._attached_session = "scratch"
        client._attached_at = 1000.0
        self.assertEqual(client.quiet_seconds("%9", now=1007.0), 7.0)

    def test_bad_output_lines_are_ignored(self):
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        for line in ("%output", "%output %", "%output not-a-pane x"):
            client.handle_line(line, now=1000.0)
        self.assertIsNone(client.quiet_seconds("%0", now=1001.0))


# ---------------------------------------------------------------------------
# Live control mode against a scratch tmux server (sandbox only)
# ---------------------------------------------------------------------------

class LiveControlModeTests(_TempHome):
    def setUp(self):
        super().setUp()
        if shutil.which("tmux") is None:
            self.skipTest("no tmux binary in the sandbox")
        self.sock = str(Path(self._tmp.name) / "tmux.sock")
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d",
                        "-s", "watcher-scratch", "-x", "80", "-y", "24",
                        "sleep 30"], check=True)
        self._env = mock.patch.dict(
            os.environ, {"TMUXWEB_AGENT_TMUX_SOCKET": self.sock})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._kill_server)

    def _kill_server(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"],
                       capture_output=True)

    def test_output_resets_quiet_and_silence_accumulates(self):
        watcher = self.watcher(quiet_seconds=2.0)
        try:
            # Attach through the watcher path and prove %output lands.
            self.assertTrue(watcher._control.ensure_started("/usr/bin/tmux"))
            self.assertTrue(watcher._control.attach("watcher-scratch"))
            subprocess.run(["tmux", "-S", self.sock, "send-keys",
                            "-t", "watcher-scratch", "echo live-output",
                            "Enter"], check=True)
            deadline = time.monotonic() + 5
            seen = None
            while time.monotonic() < deadline:
                seen = watcher._control.quiet_seconds("%0")
                if seen is not None and seen < 2.0:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(seen)
            self.assertLess(seen, 2.0)
            # Then silence: after the quiet threshold, quiet holds.
            time.sleep(2.2)
            self.assertGreaterEqual(
                watcher._control.quiet_seconds("%0"), 2.0)
            self.assertTrue(watcher._control_quiet(
                {"pane_id": "%0"}, time.time()))
        finally:
            watcher.stop()

    def test_no_sessions_means_no_control_client(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"],
                       capture_output=True)
        watcher = self.watcher()
        try:
            self.assertFalse(watcher._control.ensure_started("/usr/bin/tmux"))
            self.assertFalse(watcher._control_quiet(
                {"pane_id": "%0"}, time.time()))
        finally:
            watcher.stop()


# ---------------------------------------------------------------------------
# The needs_input op
# ---------------------------------------------------------------------------

class NeedsInputOpTests(_TempHome):
    def test_op_round_trips_through_handle_batch(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        reply = agent_program.handle_batch(
            {"id": "ni-1", "ops": [{"op": "needs_input", "panes": [
                {"pane_id": "%0", "provider": "claude", "cwd": "/proj",
                 "session_id": "sess-1"}]}]}, self.resolver)
        self.assertEqual(reply["id"], "ni-1")
        self.assertTrue(reply["ok"])
        result = reply["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["agent_version"], agent_program.VERSION)
        self.assertTrue(result["waiting"]["%0"]["waiting"])
        self.assertEqual(result["waiting"]["%0"]["source"], "hook")

    def test_op_rejects_a_missing_panes_list(self):
        self.assertFalse(agent_program.run_op(
            {"op": "needs_input"}, self.resolver)["ok"])
        self.assertFalse(agent_program.run_op(
            {"op": "needs_input", "panes": "%0"}, self.resolver)["ok"])

    def test_op_with_no_signal_reports_nothing_waiting(self):
        reply = agent_program.handle_batch(
            {"id": "ni-2", "ops": [{"op": "needs_input", "panes": [
                {"pane_id": "%0", "provider": "hermes", "cwd": "/proj"}]}]},
            self.resolver)
        result = reply["results"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["waiting"]["%0"]["waiting"])

    def test_needs_input_op_alongside_exec_in_one_batch(self):
        reply = agent_program.handle_batch(
            {"id": "ni-3", "ops": [
                {"op": "exec", "command": "echo hi", "timeout": 5},
                {"op": "needs_input", "panes": []}]}, self.resolver)
        self.assertTrue(reply["ok"])
        self.assertEqual(len(reply["results"]), 2)
        self.assertEqual(
            base64.b64decode(reply["results"][0]["stdout_b64"]), b"hi\n")
        self.assertEqual(reply["results"][1]["waiting"], {})


class DaemonNeedsInputTests(_TempHome):
    """The daemon answers needs_input over a real WebSocket (127.0.0.1).

    Reuses the minimal-server shape from test_agent_program: the fake
    server speaks the agent's own framing, so no real host is involved.
    """

    def test_daemon_answers_needs_input_then_auth_fails(self):
        import struct as _struct
        from tests.test_agent_program import (  # noqa: F401  (test-only reuse)
            _MiniWS, _accept_key, _handshake_101)
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        seen = {}

        def handler(conn):
            _handshake_101(conn)
            opcode, payload = conn.read_frame()
            seen["hello"] = json.loads(payload.decode("utf-8"))
            self.assertEqual(seen["hello"]["agent"]["version"], "1.1.0")
            conn.send_text(json.dumps({
                "id": "req-ni",
                "ops": [{"op": "needs_input", "panes": [
                    {"pane_id": "%0", "provider": "claude", "cwd": "/proj",
                     "session_id": "sess-1"}]}]}))
            opcode, payload = conn.read_frame()
            seen["reply"] = json.loads(payload.decode("utf-8"))
            conn.send_close(agent_program.CLOSE_AUTH_FAILED)

        with _MiniWS(handler) as server:
            code = agent_program.run_daemon(
                f"ws://127.0.0.1:{server.port}/ws/agent", "good-token",
                tmux="/usr/bin/tmux")
        self.assertEqual(code, 3)
        result = seen["reply"]["results"][0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["waiting"]["%0"]["waiting"])
        self.assertEqual(result["waiting"]["%0"]["source"], "hook")


# ---------------------------------------------------------------------------
# Hook script
# ---------------------------------------------------------------------------

class HookScriptTests(_TempHome):
    def _run_hook(self, event_arg, payload, env_extra=None):
        events = self.home / "events.jsonl"
        env = dict(os.environ, TMUXWEB_AGENT_EVENTS=str(events))
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT), event_arg],
            input=json.dumps(payload), text=True, capture_output=True,
            env=env, timeout=10)
        return proc, events

    def test_hook_appends_one_line_and_exits_zero(self):
        proc, events = self._run_hook("PermissionRequest", {
            "hook_event_name": "PermissionRequest", "session_id": "s-1",
            "cwd": "/proj"})
        self.assertEqual(proc.returncode, 0)
        lines = events.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        line = json.loads(lines[0])
        self.assertEqual(line["hook_event_name"], "PermissionRequest")
        self.assertEqual(line["session_id"], "s-1")
        self.assertEqual(line["cwd"], "/proj")
        self.assertIn("timestamp", line)

    def test_hook_is_fail_open_on_garbage_stdin(self):
        events = self.home / "events.jsonl"
        env = dict(os.environ, TMUXWEB_AGENT_EVENTS=str(events))
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT), "Stop"],
            input="{garbage", text=True, capture_output=True, env=env,
            timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_hook_with_no_event_name_writes_nothing_but_exits_zero(self):
        events = self.home / "events.jsonl"
        env = dict(os.environ, TMUXWEB_AGENT_EVENTS=str(events))
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="{}", text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(events.exists())


# ---------------------------------------------------------------------------
# Server side: transport + sweep merge
# ---------------------------------------------------------------------------

class NeedsInputTransportTests(unittest.TestCase):
    def _transport(self, packet=None, error=None):
        registry = mock.Mock()
        if error is not None:
            registry.call.side_effect = error
        else:
            registry.call.return_value = packet
        return agent_transport.AgentTransport(
            {"id": "h1", "host": "example.invalid"}, registry), registry

    def test_needs_input_many_returns_the_agents_waiting_map(self):
        packet = {"id": "x", "ok": True, "results": [{"ok": True, "waiting": {
            "%0": {"waiting": True, "source": "hook", "since": 123.0}}}]}
        transport, registry = self._transport(packet)
        out = transport.needs_input_many(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}])
        self.assertEqual(out["%0"]["source"], "hook")
        sent = registry.call.call_args[0][1]
        self.assertEqual(sent[0]["op"], "needs_input")
        self.assertEqual(sent[0]["panes"][0]["pane_id"], "%0")

    def test_old_agent_error_result_degrades_to_empty(self):
        packet = {"id": "x", "ok": True, "results": [
            {"ok": False, "error": "unknown op: 'needs_input'"}]}
        transport, _ = self._transport(packet)
        self.assertEqual(transport.needs_input_many(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]), {})

    def test_no_agent_degrades_to_empty(self):
        transport, _ = self._transport(
            error=agent_transport.AgentUnavailable("no agent"))
        self.assertEqual(transport.needs_input_many(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]), {})

    def test_timeout_degrades_to_empty(self):
        transport, _ = self._transport(error=RuntimeError("timed out"))
        self.assertEqual(transport.needs_input_many(
            [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]), {})

    def test_empty_panes_never_calls_the_agent(self):
        transport, registry = self._transport({"ok": True, "results": []})
        self.assertEqual(transport.needs_input_many([]), {})
        registry.call.assert_not_called()

    def test_registry_accepts_needs_input_and_still_rejects_other_ops(self):
        registry = agent_transport.AgentRegistry()
        with self.assertRaises(agent_transport.AgentUnavailable):
            registry.call("nope", [{"op": "needs_input", "panes": []}])
        with self.assertRaises(ValueError):
            registry.call("nope", [{"op": "needs_input"}])
        with self.assertRaises(ValueError):
            registry.call("nope", [{"op": "rm", "command": "x"}])


class MergeTests(unittest.TestCase):
    def test_agent_event_wins_when_present(self):
        self.assertTrue(tmux_web.effective_decision_point(False, False, True))
        self.assertTrue(tmux_web.effective_decision_point(True, False, False))
        self.assertTrue(tmux_web.effective_decision_point(False, True, False))

    def test_agent_silence_changes_nothing(self):
        self.assertFalse(tmux_web.effective_decision_point(False, False, False))
        self.assertFalse(tmux_web.effective_decision_point(False, False, None))

    def test_source_extraction_only_accepts_known_sources(self):
        self.assertEqual(tmux_web._agent_waiting_source(
            {"waiting": True, "source": "hook"}), "hook")
        self.assertEqual(tmux_web._agent_waiting_source(
            {"waiting": True, "source": "control-mode"}), "control-mode")
        self.assertIsNone(tmux_web._agent_waiting_source(
            {"waiting": True, "source": "guessed"}))
        self.assertIsNone(tmux_web._agent_waiting_source(
            {"waiting": False, "source": "hook"}))
        self.assertIsNone(tmux_web._agent_waiting_source(None))

    def test_merge_prefers_the_host_scoped_key(self):
        waiting = {"h1~%0": {"waiting": True, "source": "hook"},
                   "%0": {"waiting": False}}
        self.assertEqual(
            tmux_web._merge_agent_waiting(waiting, "h1", "%0")["source"], "hook")
        self.assertIsNone(tmux_web._merge_agent_waiting(waiting, "h2", "%0"))

    def test_descriptor_carries_what_the_watcher_needs(self):
        descriptor = tmux_web._pane_needs_input_descriptor(
            {"pane_id": "%0", "provider": "claude", "path": "/proj",
             "title": "t"})
        self.assertEqual(descriptor, {"pane_id": "%0", "provider": "claude",
                                      "cwd": "/proj", "title": "t"})


if __name__ == "__main__":
    unittest.main()
