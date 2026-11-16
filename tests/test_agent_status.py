import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import agent_status


def write_session(directory: Path, name: str, records, age_seconds: int = 0) -> Path:
    path = directory / name
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


CLAUDE_IDLE = {"type": "system", "subtype": "turn_duration", "durationMs": 1500}
CLAUDE_RUNNING = {"type": "assistant", "message": {"role": "assistant",
                                                   "content": [{"type": "tool_use", "name": "Bash"}]}}
CODEX_IDLE = {"type": "event_msg", "payload": {"type": "task_complete"}}
CODEX_RUNNING = {"type": "event_msg", "payload": {"type": "task_started"}}
CODEX_NOISE = {"type": "event_msg", "payload": {"type": "thread_settings_applied"}}
KIMI_IDLE = {"type": "prompt.completed", "time": 1}
KIMI_RUNNING = {"type": "agent.message.appended", "time": 2}


class AgentStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_a_finished_turn_is_idle(self):
        path = write_session(self.root, "claude.jsonl", [{"type": "user"}, CLAUDE_IDLE])
        self.assertEqual(agent_status.status_for_file("claude", path), agent_status.IDLE)
        path = write_session(self.root, "codex.jsonl", [{"type": "task_started"}, CODEX_IDLE])
        self.assertEqual(agent_status.status_for_file("codex", path), agent_status.IDLE)
        path = write_session(self.root, "kimi.jsonl", [KIMI_RUNNING, KIMI_IDLE])
        self.assertEqual(agent_status.status_for_file("kimi", path), agent_status.IDLE)

    def test_a_running_turn_is_busy(self):
        now = time.time()
        for provider, records in (("claude", [CLAUDE_IDLE, {"type": "user"}, CLAUDE_RUNNING]),
                                  ("codex", [CODEX_IDLE, CODEX_RUNNING]),
                                  ("kimi", [KIMI_IDLE, KIMI_RUNNING])):
            path = write_session(self.root, f"{provider}.jsonl", records)
            os.utime(path, (now, now))          # a live turn keeps writing
            self.assertEqual(agent_status.status_for_file(provider, path), agent_status.BUSY, provider)

    def test_bookkeeping_after_the_turn_does_not_hide_the_verdict(self):
        """codex appends thread_settings_applied after task_complete."""
        path = write_session(self.root, "codex.jsonl", [CODEX_IDLE, CODEX_NOISE, CODEX_NOISE])
        self.assertEqual(agent_status.status_for_file("codex", path), agent_status.IDLE)

    def test_a_half_written_last_line_is_ignored(self):
        path = self.root / "kimi.jsonl"
        path.write_text(json.dumps(KIMI_IDLE) + "\n" + '{"type":"agent.message.appe')
        self.assertEqual(agent_status.status_for_file("kimi", path), agent_status.IDLE)

    def test_a_stale_busy_file_is_not_busy_forever(self):
        """An aborted session ends on agent.message.appended and never completes."""
        path = write_session(self.root, "kimi.jsonl", [KIMI_RUNNING], age_seconds=agent_status.STALE_BUSY_SECONDS + 60)
        self.assertEqual(agent_status.status_for_file("kimi", path), agent_status.IDLE)

    def test_a_pane_resolves_its_own_session_file(self):
        """Resolution reuses the transcript helpers, so both always agree on the session."""
        home = self.root / "home"
        project = home / ".claude" / "projects" / "-home-user-repo"
        project.mkdir(parents=True)
        session = project / "abc.jsonl"
        session.write_text(json.dumps(CLAUDE_RUNNING) + "\n")
        self.assertEqual(agent_status.status_for_pane("claude", "/home/user/repo", "", home=str(home)),
                         agent_status.BUSY)
        session.write_text(json.dumps(CLAUDE_RUNNING) + "\n" + json.dumps(CLAUDE_IDLE) + "\n")
        self.assertEqual(agent_status.status_for_pane("claude", "/home/user/repo", "", home=str(home)),
                         agent_status.IDLE)
        self.assertEqual(agent_status.status_for_pane("claude", "/home/user/other", "", home=str(home)),
                         agent_status.UNKNOWN)

    def test_unknown_when_nothing_is_decided(self):
        path = write_session(self.root, "codex.jsonl", [CODEX_NOISE])
        self.assertEqual(agent_status.status_for_file("codex", path), agent_status.UNKNOWN)
        self.assertEqual(agent_status.status_for_file("claude", self.root / "missing.jsonl"), agent_status.UNKNOWN)
        self.assertEqual(agent_status.status_for_file("shell", path), agent_status.UNKNOWN)

    def test_the_shell_rules_agree_with_python(self):
        """The same rules run on remote hosts (no python3 there), so they must match."""
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        cases = []
        now = time.time()
        for provider, records, age in (
            ("claude", [CLAUDE_IDLE], 0),
            ("claude", [{"type": "user"}, CLAUDE_RUNNING], 0),
            ("codex", [CODEX_RUNNING], 0),
            ("codex", [CODEX_IDLE, CODEX_NOISE], 0),
            ("kimi", [KIMI_IDLE], 0),
            ("kimi", [KIMI_RUNNING], 0),
            ("kimi", [KIMI_RUNNING], agent_status.STALE_BUSY_SECONDS + 60),
        ):
            path = write_session(self.root, f"{provider}-{len(cases)}.jsonl", records)
            if age:
                os.utime(path, (now - age, now - age))
            cases.append((provider, path))
        script = agent_status.STATUS_SHELL + '\nstatus_of "$1" "$2"\n'
        for provider, path in cases:
            expected = agent_status.status_for_file(provider, path)
            shell = subprocess.run(["bash", "-c", script, "sh", provider, str(path)],
                                   capture_output=True, text=True).stdout.strip()
            self.assertEqual(shell, expected, f"{provider} {path.name}")


def claude_assistant(tool_id: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}]}}


def claude_result(tool_id: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def claude_text_user(text: str = "go on") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def codex_call(call_id: str) -> dict:
    return {"type": "response_item", "payload": {
        "type": "function_call", "id": "item-" + call_id,
        "call_id": call_id, "name": "shell", "arguments": "{}"}}


def codex_output(call_id: str) -> dict:
    return {"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": call_id, "output": "ok"}}


def codex_event(payload_type: str) -> dict:
    return {"type": "event_msg", "payload": {"type": payload_type}}


def kimi_event(kind: str, **extra) -> dict:
    record = {"type": kind, "time": 1}
    record.update(extra)
    return record


class DecisionPointTests(unittest.TestCase):
    """Leg 1, corrected: the turn-end marker never fires during a permission
    wait (0/4008 tool_use blocks in real Claude files), so "decision point"
    is turn-ended OR a tool call outstanding."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_a_pending_tool_call_is_a_decision_point(self):
        """The permission-wait shape: assistant issued the call, no result yet."""
        path = write_session(self.root, "claude.jsonl",
                             [{"type": "user"}, claude_assistant("toolu_1")])
        self.assertEqual(agent_status.status_for_file("claude", path), agent_status.BUSY)
        self.assertTrue(agent_status.decision_point_for_file("claude", path))
        path = write_session(self.root, "codex.jsonl",
                             [codex_event("task_started"), codex_call("call_1")])
        self.assertTrue(agent_status.decision_point_for_file("codex", path))
        path = write_session(self.root, "kimi.jsonl",
                             [kimi_event("prompt.submitted"), kimi_event("tool.call", uuid="t1")])
        self.assertTrue(agent_status.decision_point_for_file("kimi", path))

    def test_a_completed_tool_call_is_not_a_decision_point(self):
        path = write_session(self.root, "claude.jsonl",
                             [claude_assistant("toolu_1"), claude_result("toolu_1"),
                              {"type": "assistant", "message": {"role": "assistant",
                                                               "content": [{"type": "text", "text": "done"}]}}])
        self.assertFalse(agent_status.decision_point_for_file("claude", path))
        path = write_session(self.root, "codex.jsonl",
                             [codex_call("call_1"), codex_output("call_1"),
                              codex_event("item_completed")])
        self.assertFalse(agent_status.decision_point_for_file("codex", path))
        path = write_session(self.root, "kimi.jsonl",
                             [kimi_event("tool.call", uuid="t1"), kimi_event("step.end")])
        self.assertFalse(agent_status.decision_point_for_file("kimi", path))

    def test_a_turn_end_is_still_a_decision_point(self):
        """The old leg-1 case keeps working: turn ended, no call outstanding."""
        path = write_session(self.root, "claude.jsonl", [{"type": "user"}, CLAUDE_IDLE])
        self.assertTrue(agent_status.decision_point_for_file("claude", path))
        path = write_session(self.root, "kimi.jsonl", [KIMI_IDLE])
        self.assertTrue(agent_status.decision_point_for_file("kimi", path))

    def test_parallel_calls_need_every_result_back(self):
        """Two calls out, one result back: the other is still outstanding."""
        path = write_session(self.root, "claude.jsonl",
                             [claude_assistant("toolu_1"), claude_assistant("toolu_2"),
                              claude_result("toolu_1")])
        self.assertTrue(agent_status.decision_point_for_file("claude", path))
        path = write_session(self.root, "claude.jsonl",
                             [claude_assistant("toolu_1"), claude_assistant("toolu_2"),
                              claude_result("toolu_1"), claude_result("toolu_2")])
        self.assertFalse(agent_status.decision_point_for_file("claude", path))

    def test_a_user_message_after_the_call_abandons_it(self):
        """The user typed something new instead of answering the tool: no longer pending."""
        path = write_session(self.root, "claude.jsonl",
                             [claude_assistant("toolu_1"), claude_text_user("stop that")])
        self.assertFalse(agent_status.decision_point_for_file("claude", path))
        path = write_session(self.root, "codex.jsonl",
                             [codex_call("call_1"), codex_event("task_complete")])
        self.assertFalse(agent_status.has_outstanding_call("codex", path))

    def test_a_kimi_tool_call_closes_on_the_next_llm_request(self):
        """llm.request only fires once the tool result is back."""
        path = write_session(self.root, "kimi.jsonl",
                             [kimi_event("tool.call", uuid="t1"), kimi_event("llm.request")])
        self.assertFalse(agent_status.decision_point_for_file("kimi", path))
        path = write_session(self.root, "kimi.jsonl",
                             [kimi_event("tool.call", uuid="t1"),
                              kimi_event("context.append_loop_event",
                                         event={"type": "step.end"})])
        self.assertFalse(agent_status.decision_point_for_file("kimi", path))
        # an assistant message after the call does NOT close it: the summary
        # is written after the result, but the message alone proves nothing
        path = write_session(self.root, "kimi.jsonl",
                             [kimi_event("tool.call", uuid="t1"),
                              kimi_event("agent.message.appended")])
        self.assertTrue(agent_status.decision_point_for_file("kimi", path))

    def test_providers_without_a_signal_never_decide(self):
        path = write_session(self.root, "x.jsonl", [claude_assistant("toolu_1")])
        self.assertFalse(agent_status.has_outstanding_call("hermes", path))
        self.assertFalse(agent_status.has_outstanding_call("shell", path))
        self.assertFalse(agent_status.decision_point_for_file("hermes", path))
        self.assertFalse(agent_status.decision_point_for_file("claude", self.root / "missing.jsonl"))

    def test_a_pane_resolves_its_own_session_file_for_the_decision(self):
        home = self.root / "home"
        project = home / ".claude" / "projects" / "-home-user-repo"
        project.mkdir(parents=True)
        session = project / "abc.jsonl"
        session.write_text(json.dumps(claude_assistant("toolu_9")) + "\n")
        self.assertTrue(agent_status.decision_point_for_pane(
            "claude", "/home/user/repo", "", home=str(home)))
        session.write_text(json.dumps(claude_assistant("toolu_9")) + "\n"
                           + json.dumps(claude_result("toolu_9")) + "\n")
        self.assertFalse(agent_status.decision_point_for_pane(
            "claude", "/home/user/repo", "", home=str(home)))
        self.assertFalse(agent_status.decision_point_for_pane(
            "claude", "/home/user/other", "", home=str(home)))

    def test_the_shell_decision_rules_agree_with_python(self):
        """decision_of runs on remote hosts (no python3 there), so it must match."""
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        cases = [
            ("claude", [claude_assistant("toolu_1")], True),
            ("claude", [claude_assistant("toolu_1"), claude_result("toolu_1")], False),
            ("claude", [{"type": "user"}, CLAUDE_IDLE], True),
            ("claude", [claude_assistant("toolu_1"), claude_text_user()], False),
            ("claude", [claude_assistant("toolu_1"),
                        {"type": "system", "subtype": "turn_duration"}], True),
            ("codex", [codex_call("call_1")], True),
            ("codex", [codex_call("call_1"), codex_output("call_1")], False),
            ("codex", [codex_call("call_1"), codex_event("task_complete")], True),
            ("codex", [{"type": "response_item", "payload": {
                "type": "custom_tool_call", "call_id": "c9", "name": "x"}},
                       {"type": "response_item", "payload": {
                           "type": "custom_tool_call_output", "call_id": "c9"}}], False),
            ("kimi", [kimi_event("tool.call", uuid="t1")], True),
            ("kimi", [kimi_event("tool.call", uuid="t1"), kimi_event("step.end")], False),
            ("kimi", [kimi_event("tool.call", uuid="t1"), kimi_event("prompt.completed")], True),
            ("kimi", [kimi_event("prompt.submitted")], False),
            ("hermes", [claude_assistant("toolu_1")], False),
        ]
        script = agent_status.STATUS_SHELL + '\ndecision_of "$1" "$2"\n'
        for index, (provider, records, expected) in enumerate(cases):
            path = write_session(self.root, f"dec-{index}.jsonl", records)
            want = agent_status.decision_point_for_file(provider, path)
            self.assertEqual(want, expected, f"python disagrees with the case: {provider} {records}")
            shell = subprocess.run(["bash", "-c", script, "sh", provider, str(path)],
                                   capture_output=True, text=True)
            self.assertEqual(shell.stdout.strip(), "yes" if expected else "no",
                             f"shell disagrees: {provider} {records}\n{shell.stderr}")
            self.assertEqual(shell.returncode, 0)


if __name__ == "__main__":
    unittest.main()
