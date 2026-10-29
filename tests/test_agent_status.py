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


if __name__ == "__main__":
    unittest.main()
