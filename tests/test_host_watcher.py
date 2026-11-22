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
import signal
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

    def test_same_directory_cannot_override_another_session_or_pane(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest", "session_id": "a",
                           "cwd": "/proj", "timestamp": now}])
        panes = [{"pane_id": "%" + str(i), "provider": "claude", "session_id": sid, "cwd": "/proj"}
                 for i, sid in enumerate(("a", "b", ""))]
        states = self.watcher().snapshot(panes, now=now)
        self.assertEqual([states[p["pane_id"]]["hook"] for p in panes], [True, False, False])

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
# Caching (Fix 2)
# ---------------------------------------------------------------------------

class CacheTests(_TempHome):
    def _claude_waiting_file(self, cwd="/proj"):
        project = self.home / ".claude" / "projects" / "-proj"
        return _write_jsonl(project / "sess-1.jsonl", [
            {"type": "user"}, _claude_assistant()])

    def _resolve_spy(self):
        orig = agent_program._watcher_resolve_session_file
        return mock.patch.object(
            agent_program, "_watcher_resolve_session_file", wraps=orig)

    def test_snapshot_cache_hit_skips_recompute(self):
        self._claude_waiting_file()
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]
        watcher = self.watcher()
        with self._resolve_spy() as spy:
            first = watcher.snapshot(panes)
            second = watcher.snapshot(panes)
        self.assertEqual(first, second)
        self.assertEqual(spy.call_count, 1)  # the second snapshot reused it

    def test_snapshot_cache_miss_on_a_different_pane_set(self):
        self._claude_waiting_file()
        watcher = self.watcher()
        with self._resolve_spy() as spy:
            watcher.snapshot([{"pane_id": "%0", "provider": "claude",
                               "cwd": "/proj"}])
            out = watcher.snapshot([
                {"pane_id": "%0", "provider": "claude", "cwd": "/proj"},
                {"pane_id": "%1", "provider": "claude", "cwd": "/other"}])
        self.assertEqual(spy.call_count, 2)  # a new pane set recomputes
        self.assertIn("%1", out)

    def test_snapshot_cache_expires(self):
        self._claude_waiting_file()
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]
        watcher = self.watcher(poll_interval=0.5)
        with mock.patch.object(watcher, "_control_quiet",
                               wraps=watcher._control_quiet) as spy:
            watcher.snapshot(panes)
            watcher.snapshot(panes)
            self.assertEqual(spy.call_count, 1)  # cache hit: no recompute
            time.sleep(0.6)
            watcher.snapshot(panes)
            self.assertEqual(spy.call_count, 2)  # expired: recomputed

    def test_needs_input_op_reuses_the_poll_loop_snapshot(self):
        """The op path shares the poll loop's snapshot: a sweep that
        just ran must not pay for a second full snapshot."""
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
                  "session_id": "sess-1"}]
        watcher = self.watcher()
        with self._resolve_spy() as spy:
            watcher.snapshot(panes, now=now)  # the poll loop just ran
            result = agent_program.run_needs_input(
                {"op": "needs_input", "panes": panes}, self.resolver,
                watcher)
        self.assertEqual(spy.call_count, 1)
        self.assertTrue(result["waiting"]["%0"]["waiting"])

    def test_resolve_cache_survives_a_snapshot_cache_bust(self):
        self._claude_waiting_file()
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]
        watcher = self.watcher()
        with self._resolve_spy() as spy:
            watcher.snapshot(panes)
            watcher._snapshot_cache = None
            watcher.snapshot(panes)
        self.assertEqual(spy.call_count, 1)

    def test_resolve_cache_invalidates_when_the_file_changes(self):
        path = self._claude_waiting_file()
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj"}]
        watcher = self.watcher()
        with self._resolve_spy() as spy:
            watcher.snapshot(panes)
            watcher._snapshot_cache = None
            later = time.time() + 60
            os.utime(path, (later, later))
            watcher.snapshot(panes)
        self.assertEqual(spy.call_count, 2)

    def test_resolve_miss_is_cached_briefly_then_retried(self):
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/nowhere"}]
        watcher = self.watcher()
        with self._resolve_spy() as spy:
            watcher.snapshot(panes)
            watcher._snapshot_cache = None
            watcher.snapshot(panes)
            self.assertEqual(spy.call_count, 1)  # miss cached (5s)
            key = ("claude", "/nowhere", "")
            cached_path, cached_at, mtime = watcher._resolve_cache[key]
            watcher._resolve_cache[key] = (cached_path, cached_at - 6.0, mtime)
            watcher._snapshot_cache = None
            watcher.snapshot(panes)
            self.assertEqual(spy.call_count, 2)  # miss TTL expired

    def test_event_file_is_not_reparsed_while_unchanged(self):
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
                  "session_id": "sess-1"}]
        watcher = self.watcher()
        orig_parse = agent_program.parse_hook_event
        with mock.patch.object(agent_program, "parse_hook_event",
                               wraps=orig_parse) as spy:
            out = watcher.snapshot(panes, now=now)
            self.assertTrue(out["%0"]["waiting"])
            first_calls = spy.call_count
            self.assertGreater(first_calls, 0)
            watcher._snapshot_cache = None
            out = watcher.snapshot(panes, now=now)
            self.assertTrue(out["%0"]["waiting"])
            self.assertEqual(spy.call_count, first_calls)  # no re-parse
            # A new event changes mtime/size: the next snapshot sees it.
            with open(watcher.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"hook_event_name": "Stop", "session_id": "sess-1",
                     "cwd": "/proj", "timestamp": now}) + "\n")
            watcher._snapshot_cache = None
            out = watcher.snapshot(panes, now=now)
            self.assertFalse(out["%0"]["waiting"])
            self.assertGreater(spy.call_count, first_calls)

    def test_stale_events_still_expire_from_the_cached_parse(self):
        """The 1h TTL is applied at use time, so a reused parse still
        lets an event expire on schedule without re-reading the file."""
        now = time.time()
        self.events_file([{"hook_event_name": "PermissionRequest",
                           "session_id": "sess-1", "cwd": "/proj",
                           "timestamp": now}])
        panes = [{"pane_id": "%0", "provider": "claude", "cwd": "/proj",
                  "session_id": "sess-1"}]
        watcher = self.watcher()
        orig_parse = agent_program.parse_hook_event
        with mock.patch.object(agent_program, "parse_hook_event",
                               wraps=orig_parse) as spy:
            out = watcher.snapshot(panes, now=now)
            self.assertTrue(out["%0"]["waiting"])
            watcher._snapshot_cache = None
            out = watcher.snapshot(panes, now=now + 3700)
            self.assertFalse(out["%0"]["waiting"])
            self.assertEqual(spy.call_count, 1)  # parsed once, reused


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

    def test_stale_entry_for_a_pane_in_another_session_is_unknown(self):
        """Fix 1(a): attached to A, stale %output for a pane in B.

        The old code returned now - last for any recorded entry,
        whatever session the client was attached to — so a pane in
        B read as quiet on B's unobserved output. Now it is unknown.
        """
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        client.handle_line("%output %0 hello", now=1000.0)
        client._attached_session = "sess-a"
        client._attached_at = 999.0
        self.assertIsNone(client.quiet_seconds("%0", now=1010.0,
                                               session="sess-b"))
        # The same entry, judged against its own session, still counts.
        self.assertEqual(client.quiet_seconds("%0", now=1010.0,
                                              session="sess-a"), 10.0)

    def test_no_entry_for_a_pane_in_another_session_is_unknown(self):
        """Fix 1(b): no output, attached to A, pane in B -> unknown."""
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        client._attached_session = "sess-a"
        client._attached_at = 1000.0
        self.assertIsNone(client.quiet_seconds("%0", now=1010.0,
                                               session="sess-b"))

    def test_pane_in_attached_session_with_no_entry_is_quiet_since_attach(self):
        """Fix 1(c): no output while attached -> quiet since attach."""
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        client._attached_session = "sess-a"
        client._attached_at = 1000.0
        self.assertEqual(client.quiet_seconds("%0", now=1007.0,
                                              session="sess-a"), 7.0)

    def test_entry_older_than_the_attach_counts_as_quiet_since_attach(self):
        """An entry predating the current attach is stale: the client
        only observes output while attached, so quiet runs from the
        attach, not from an entry a previous attachment recorded."""
        client = agent_program._ControlModeClient("/usr/bin/tmux")
        client.handle_line("%output %0 hello", now=900.0)
        client._attached_session = "sess-a"
        client._attached_at = 1000.0
        self.assertEqual(client.quiet_seconds("%0", now=1010.0,
                                              session="sess-a"), 10.0)


class QuietSessionValidationTests(_TempHome):
    """Fix 1(d) at watcher level: the session comes from the map."""

    def _fake_attached_client(self, watcher, session, attached_at):
        """A client that looks alive and attached, without any tmux."""
        client = agent_program._ControlModeClient(
            "/usr/bin/tmux", last_output=watcher._shared_output,
            lock=watcher._shared_output_lock)
        proc = mock.Mock()
        proc.poll.return_value = None
        client._proc = proc
        client._started_ok = True
        client._attached_session = session
        client._attached_at = attached_at
        return client

    def test_pane_with_no_session_in_the_map_reads_unknown(self):
        watcher = self.watcher()
        watcher._pane_sessions = {"%0": "sess-a"}
        watcher._pane_sessions_at = time.monotonic()
        watcher._controls["sess-a"] = self._fake_attached_client(
            watcher, "sess-a", time.time() - 10)
        try:
            self.assertFalse(watcher._control_quiet(
                {"pane_id": "%9"}, time.time()))
            self.assertTrue(watcher._control_quiet(
                {"pane_id": "%0"}, time.time()))
        finally:
            watcher.stop()

    def test_panes_in_different_sessions_are_judged_independently(self):
        """%0 in A (quiet 10s) and %1 in B (attached 1s ago): the two
        panes must not share one verdict — and a stale shared-dict
        entry for %1, predating B's attach, must not make %1 quiet."""
        watcher = self.watcher()
        watcher._pane_sessions = {"%0": "sess-a", "%1": "sess-b"}
        watcher._pane_sessions_at = time.monotonic()
        now = time.time()
        watcher._controls["sess-a"] = self._fake_attached_client(
            watcher, "sess-a", now - 10)
        watcher._controls["sess-b"] = self._fake_attached_client(
            watcher, "sess-b", now - 1)
        watcher._shared_output["%1"] = now - 100
        try:
            self.assertTrue(watcher._control_quiet(
                {"pane_id": "%0"}, now))
            self.assertFalse(watcher._control_quiet(
                {"pane_id": "%1"}, now))
        finally:
            watcher.stop()


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
        project = self.home / ".claude" / "projects" / "-proj"
        _write_jsonl(project / "sess-1.jsonl", [
            {"type": "user"}, _claude_assistant()])
        watcher = self.watcher(quiet_seconds=2.0)
        try:
            # Drive through the watcher path: the snapshot spawns one
            # client for watcher-scratch and attaches it there.
            out = watcher.snapshot([{"pane_id": "%0", "provider": "claude",
                                     "cwd": "/proj"}])
            self.assertTrue(out["%0"]["decision_point"])
            client = watcher._controls["watcher-scratch"]
            self.assertTrue(client.available)
            self.assertEqual(client.attached_session, "watcher-scratch")
            # Attach through the watcher path and prove %output lands.
            subprocess.run(["tmux", "-S", self.sock, "send-keys",
                            "-t", "watcher-scratch", "echo live-output",
                            "Enter"], check=True)
            deadline = time.monotonic() + 5
            seen = None
            while time.monotonic() < deadline:
                seen = client.quiet_seconds(
                    "%0", session="watcher-scratch")
                if seen is not None and seen < 2.0:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(seen)
            self.assertLess(seen, 2.0)
            # Then silence: after the quiet threshold, quiet holds.
            time.sleep(2.2)
            self.assertGreaterEqual(
                client.quiet_seconds("%0", session="watcher-scratch"), 2.0)
            self.assertTrue(watcher._control_quiet(
                {"pane_id": "%0"}, time.time()))
        finally:
            watcher.stop()

    def test_no_sessions_means_no_control_client(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"],
                       capture_output=True)
        watcher = self.watcher()
        try:
            self.assertFalse(watcher._control_quiet(
                {"pane_id": "%0"}, time.time()))
            self.assertEqual(watcher._controls, {})
        finally:
            watcher.stop()


# ---------------------------------------------------------------------------
# Multi-session control-mode clients with a cap (Fix 3)
# ---------------------------------------------------------------------------

class ControlClientCapTests(_TempHome):
    """Fix 3 at unit level: the cap and the lifecycle, no tmux."""

    def _fake_client(self, watcher, session, attached_at=None):
        client = agent_program._ControlModeClient(
            "/usr/bin/tmux", last_output=watcher._shared_output,
            lock=watcher._shared_output_lock)
        proc = mock.Mock()
        proc.poll.return_value = None
        client._proc = proc
        client._started_ok = True
        client._attached_session = session
        client._attached_at = attached_at or time.time()
        return client

    def test_fifth_session_gets_no_client_and_reads_unknown(self):
        watcher = self.watcher(max_control_clients=2)
        watcher._pane_sessions = {"%0": "sess-a", "%1": "sess-b",
                                  "%2": "sess-c"}
        watcher._pane_sessions_at = time.monotonic()
        watcher._controls["sess-a"] = self._fake_client(watcher, "sess-a")
        watcher._controls["sess-b"] = self._fake_client(watcher, "sess-b")
        try:
            self.assertIsNone(watcher._ensure_control_client("sess-c"))
            self.assertFalse(watcher._control_quiet(
                {"pane_id": "%2"}, time.time()))
            self.assertEqual(len(watcher._controls), 2)
        finally:
            watcher.stop()

    def test_sync_stops_clients_for_sessions_with_no_decision_panes(self):
        watcher = self.watcher()
        a = self._fake_client(watcher, "sess-a")
        b = self._fake_client(watcher, "sess-b")
        watcher._controls["sess-a"] = a
        watcher._controls["sess-b"] = b
        with mock.patch.object(b, "stop", wraps=b.stop) as spy:
            watcher._sync_control_clients(["sess-a"])
        self.assertNotIn("sess-b", watcher._controls)
        spy.assert_called_once_with()
        self.assertIn("sess-a", watcher._controls)
        watcher.stop()

    def test_clients_share_one_last_output_dict(self):
        watcher = self.watcher()
        a = self._fake_client(watcher, "sess-a")
        b = self._fake_client(watcher, "sess-b")
        watcher._controls["sess-a"] = a
        watcher._controls["sess-b"] = b
        a.handle_line("%output %0 hello", now=1000.0)
        self.assertEqual(b._last_output["%0"], 1000.0)
        watcher.stop()

    def test_cap_default_is_four_and_env_overrides_it(self):
        self.assertEqual(agent_program.WATCHER_MAX_CONTROL_CLIENTS, 4)
        with mock.patch.dict(os.environ,
                             {"TMUXWEB_WATCHER_MAX_CONTROL_CLIENTS": ""}):
            self.assertEqual(agent_program._watcher_max_control_clients(), 4)
        with mock.patch.dict(os.environ,
                             {"TMUXWEB_WATCHER_MAX_CONTROL_CLIENTS": "2"}):
            self.assertEqual(agent_program._watcher_max_control_clients(), 2)
        with mock.patch.dict(os.environ,
                             {"TMUXWEB_WATCHER_MAX_CONTROL_CLIENTS": "bogus"}):
            self.assertEqual(agent_program._watcher_max_control_clients(), 4)
        with mock.patch.dict(os.environ,
                             {"TMUXWEB_WATCHER_MAX_CONTROL_CLIENTS": "0"}):
            self.assertEqual(agent_program._watcher_max_control_clients(), 0)


class LiveMultiSessionTests(_TempHome):
    """Fix 3 on a real scratch tmux server: cap, lifecycle, cleanup.

    Every scratch pane is a claude pane in /proj with an outstanding
    tool call, so every pane is a decision-point pane; the only thing
    separating them is the session they live in.
    """

    def setUp(self):
        super().setUp()
        if shutil.which("tmux") is None:
            self.skipTest("no tmux binary in the sandbox")
        self.sock = str(Path(self._tmp.name) / "tmux.sock")
        self._env = mock.patch.dict(
            os.environ, {"TMUXWEB_AGENT_TMUX_SOCKET": self.sock})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._kill_server)
        project = self.home / ".claude" / "projects" / "-proj"
        _write_jsonl(project / "sess-1.jsonl", [
            {"type": "user"}, _claude_assistant()])

    def _kill_server(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"],
                       capture_output=True)

    def _new_session(self, name):
        subprocess.run(["tmux", "-S", self.sock, "new-session", "-d",
                        "-s", name, "-x", "80", "-y", "24",
                        "sleep 60"], check=True)

    def _pane_map(self):
        out = subprocess.run(["tmux", "-S", self.sock, "list-panes", "-a",
                              "-F", "#{pane_id} #{session_name}"],
                             capture_output=True, text=True, check=True)
        return dict(line.split() for line in out.stdout.splitlines())

    def _panes(self, mapping):
        return [{"pane_id": pid, "provider": "claude", "cwd": "/proj"}
                for pid in mapping]

    def _control_pids(self):
        """Pids of tmux -C processes on the scratch socket."""
        out = subprocess.run(["pgrep", "-f", f"tmux -S {self.sock} -C"],
                             capture_output=True, text=True)
        return [line for line in out.stdout.splitlines() if line.strip()]

    def _control_clients(self):
        """Attached control-mode clients, per list-clients."""
        out = subprocess.run(["tmux", "-S", self.sock, "list-clients", "-F",
                              "#{client_session} #{client_control_mode}"],
                             capture_output=True, text=True)
        return [line for line in out.stdout.splitlines() if line.strip()]

    def _sessions(self):
        """Session names on the scratch server, sorted."""
        out = subprocess.run(["tmux", "-S", self.sock, "list-sessions",
                              "-F", "#{session_name}"],
                             capture_output=True, text=True, check=True)
        return sorted(out.stdout.split())

    def test_five_sessions_spawn_at_most_four_clients(self):
        for name in ("cap-a", "cap-b", "cap-c", "cap-d", "cap-e"):
            self._new_session(name)
        mapping = self._pane_map()
        self.assertEqual(len(mapping), 5)
        watcher = self.watcher(quiet_seconds=2.0)
        try:
            out = watcher.snapshot(self._panes(mapping))
            self.assertTrue(all(state["decision_point"]
                                for state in out.values()))
            self.assertEqual(len(watcher._controls), 4)
            self.assertEqual(len(self._control_pids()), 4)
            self.assertEqual(len(self._control_clients()), 4)
            # Spawning a client must not create a stray session: an
            # unattached `tmux -C` makes tmux create one (observed:
            # sessions "1", "2", "3" with live shell panes), so
            # clients spawn already attached.
            self.assertEqual(
                self._sessions(),
                ["cap-a", "cap-b", "cap-c", "cap-d", "cap-e"])
            # After 2s of silence the four covered sessions read quiet
            # (and waiting); the fifth session still has no signal.
            time.sleep(2.5)
            watcher._snapshot_cache = None
            out = watcher.snapshot(self._panes(mapping))
            quiet = [pid for pid, state in out.items()
                     if state["control_quiet"]]
            self.assertEqual(len(quiet), 4)
            self.assertEqual(len(watcher._controls), 4)
            self.assertEqual(len(self._control_pids()), 4)
            dark = [pid for pid, state in out.items()
                    if not state["control_quiet"]]
            self.assertEqual(len(dark), 1)
            self.assertFalse(out[dark[0]]["waiting"])
            self.assertTrue(all(out[pid]["waiting"] for pid in quiet))
        finally:
            watcher.stop()

    def test_stopping_the_watcher_leaves_no_control_process(self):
        for name in ("stop-a", "stop-b"):
            self._new_session(name)
        mapping = self._pane_map()
        watcher = self.watcher()
        try:
            watcher.snapshot(self._panes(mapping))
            self.assertEqual(len(self._control_pids()), 2)
            watcher.stop()
            self.assertEqual(self._control_pids(), [])
            self.assertEqual(self._control_clients(), [])
        finally:
            watcher.stop()

    def test_dead_client_restarts_lazily(self):
        self._new_session("restart-s")
        mapping = self._pane_map()
        pane_id = next(iter(mapping))
        watcher = self.watcher()
        try:
            watcher.snapshot(self._panes(mapping))
            client = watcher._controls["restart-s"]
            self.assertTrue(client.available)
            old_pid = client._proc.pid
            os.kill(old_pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while client.available and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(client.available)
            # The 10s restart discipline holds: no restart yet, so the
            # session reads as no signal.
            self.assertFalse(watcher._control_quiet(
                {"pane_id": pane_id}, time.time()))
            self.assertFalse(client.available)
            # Past the throttle, the same client object restarts (a
            # fresh attach is not quiet yet, so the op still reads
            # False — the point is the client is alive again).
            client._last_start_attempt = 0.0
            watcher._control_quiet({"pane_id": pane_id}, time.time())
            self.assertTrue(client.available)
            self.assertNotEqual(client._proc.pid, old_pid)
            self.assertEqual(client.attached_session, "restart-s")
        finally:
            watcher.stop()

    def test_client_for_a_dead_session_is_stopped(self):
        self._new_session("live-s")
        self._new_session("dead-s")
        mapping = self._pane_map()
        watcher = self.watcher()
        try:
            watcher.snapshot(self._panes(mapping))
            self.assertEqual(len(watcher._controls), 2)
            self.assertEqual(len(self._control_pids()), 2)
            self.assertEqual(self._sessions(), ["dead-s", "live-s"])
            subprocess.run(["tmux", "-S", self.sock, "kill-session",
                            "-t", "dead-s"], check=True)
            watcher._pane_sessions_at = 0.0  # force a map refresh
            watcher._snapshot_cache = None
            mapping = self._pane_map()
            self.assertEqual(list(mapping.values()), ["live-s"])
            watcher.snapshot(self._panes(mapping))
            self.assertNotIn("dead-s", watcher._controls)
            self.assertEqual(len(watcher._controls), 1)
            self.assertEqual(len(self._control_pids()), 1)
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
            self.assertEqual(seen["hello"]["agent"]["version"], agent_program.VERSION)
            conn.send_text(json.dumps({
                "type": "welcome", "host_id": "h1", "host_name": "h1",
                "protocol": "1.2.0", "next_seq": 1}))
            conn.send_text(json.dumps({
                "id": "req-ni", "seq": 1,
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
        self.assertEqual(descriptor, {"pane_id": "%0", "pane_pid": "", "provider": "claude",
                                      "cwd": "/proj", "title": "t"})


if __name__ == "__main__":
    unittest.main()
