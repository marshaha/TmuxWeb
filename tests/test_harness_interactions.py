import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import agent_models
from plugins.agent.agent import native_waits
from plugins.agent.hooks.observe import observation, record
import transcript_reader


class InteractionTests(unittest.TestCase):
    def test_structured_questions_only_in_history_and_model_only_from_context(self):
        args = {"questions": [{"question": "Which target?", "options": [{"label": "Preview"}]},
                              {"title": "Why?", "options": ["A", "B"]}]}
        call = {"type": "function_call", "name": "request_user_input", "call_id": "c1",
                "arguments": json.dumps(args)}
        message = {"type": "message", "role": "assistant", "content": "ordinary output"}
        events = [{"type": "turn_context", "payload": {"model": "real"}},
                  *[{"type": "response_item", "payload": p} for p in
                    (call, call, dict(call, name="another_tool"),
                     dict(call, call_id="bad", arguments="invalid"), message)],
                  {"type": "response_item", "payload": {"type": "function_call_output",
                                                        "model": "unrelated"}}]
        result = transcript_reader.parse_codex(map(json.dumps, events), 500, "session")
        self.assertEqual(result["model"], "real")
        self.assertEqual([m["role"] for m in result["messages"]], ["interaction", "assistant"])
        self.assertIn("• B", result["messages"][0]["text"])
        self.assertEqual(len(result["open_questions"]), 1)
        for end in ({"type": "event_msg", "payload": {"type": "turn_aborted"}},
                    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1"}}):
            self.assertEqual(transcript_reader.parse_codex(map(json.dumps, events + [end]), 10)["open_questions"], [])
        block = {"type": "tool_use", "name": "AskUserQuestion", "id": "q", "input": args}
        messages = transcript_reader.parse_claude([json.dumps(
            {"type": "assistant", "uuid": "u", "message": {"content": [
                {"type": "text", "text": "hello"}, block]}})], 10)["messages"]
        self.assertEqual([m["role"] for m in messages], ["assistant", "interaction"])

    def test_guessed_codex_rollout_never_alerts_for_another_pane(self):
        events = [{"type": "session_meta", "payload": {"id": "fixture-session", "cwd": "/shared"}},
                  {"type": "response_item", "payload": {"type": "function_call", "call_id": "q",
                   "name": "request_user_input", "arguments": {"questions": [{"question": "Which?"}]}}}]
        with tempfile.TemporaryDirectory() as home:
            file = Path(home) / ".codex/sessions/rollout-fixture-session.jsonl"
            file.parent.mkdir(parents=True)
            file.write_text("\n".join(map(json.dumps, events)))
            for hint, count in ((None, 0), ("fixture-s", 0), ("fixture-session", 1)):
                data = transcript_reader.read_codex(home, "/shared", "", 10, hint=hint)
                self.assertEqual(len(data["open_questions"]), count)
                self.assertEqual(data["messages"][0]["role"], "interaction")

    def test_observations_are_pane_scoped_and_close_only_matching_request(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / ".config/tmuxweb-agent/interactions.sqlite3"
            pane = {"pane_id": "%4", "pane_pid": "135", "provider": "hermes"}
            def write(event, rid, kind="clarify", stamp=None):
                data = {"session_id": "s1", "extra": {"platform": "tui", "request_id": rid,
                        "kind": kind, "prompt": "sensitive" if kind == "sudo" else "Which branch?"}}
                notice = observation("hermes", event, data)
                record(path, "%4", "135", "hermes", notice, stamp or time.time())
            write("on_human_input_request", "q")
            write("on_human_input_request", "pw", "sudo")
            write("on_human_input_request", "expired", stamp=time.time()-3700)
            self.assertNotIn("sensitive", path.read_bytes().decode(errors="ignore"))
            self.assertEqual(native_waits([dict(pane, pane_pid="136")], home), {})
            self.assertEqual(native_waits([dict(pane, provider="claude")], home), {})
            self.assertEqual(len(native_waits([pane], home)["%4"]), 2)
            write("on_human_input_resolved", "q")
            write("on_human_input_request", "q", stamp=time.time()-10)
            self.assertEqual([n["id"] for n in native_waits([pane], home)["%4"]], [["s1", "pw"]])

    def test_receiver_runs_from_stdin_and_only_queries_the_pane_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake = home / "tmux"
            fake.write_text("#!/bin/sh\n[ \"$*\" = 'display-message -p -t %4 #{pane_pid}' ] || exit 1\necho 135\n")
            fake.chmod(0o700)
            env = dict(os.environ, HOME=directory, PATH=directory + ":" + os.environ["PATH"],
                       TMUX_PANE="%4", TMUX="/fixture,100,1")
            receiver = Path(__file__).resolve().parents[1] / "plugins/agent/hooks/observe.py"
            panes = [{"pane_id": "%4", "pane_pid": "135", "provider": "kimi"}]
            for event, count in (("PermissionRequest", 1), ("PermissionResult", 0)):
                payload = {"hook_event_name": event, "session_id": "s", "tool_call_id": "c", "tool_name": "Read"}
                run = subprocess.run([sys.executable, str(receiver), "kimi"], env=env,
                                     input=json.dumps(payload), text=True, capture_output=True, timeout=5)
                self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
                self.assertEqual(len(native_waits(panes, home).get("%4", [])), count)

    def test_events_from_other_providers(self):
        for provider, event in (("codex", "PermissionRequest"), ("kimi", "PermissionRequest"),
                                ("gemini", "Notification"), ("claude", "Elicitation")):
            data = {"session_id": "session", "notification_type": "ToolPermission", "tool_name": "Read"}
            self.assertEqual(observation(provider, event, data)[2], "open")
            self.assertIsNone(observation(provider, "irrelevant", data))
        self.assertIsNone(observation("gemini", "Notification", {"session_id": "s", "notification_type": "idle"}))
        snap = observation("opencode", None, {"session_id": "s", "items": [{"id": "f", "kind": "form"}]})
        self.assertEqual(snap[2], "snapshot")
        self.assertEqual(snap[4], [("f", "等待填写表单")])

    def test_catalog_order_filter_reload_and_concurrent_remote_reads(self):
        with tempfile.TemporaryDirectory(prefix="catalog's ") as directory:
            root = Path(directory)
            path = root / "models_cache.json"
            rollout = root / "sessions/2026/sample.jsonl"
            models = [{"slug": name, "visibility": vis} for name, vis in (
                ("second", "list"), ("hidden", "hide"), ("first", "list"), ("first", "list"))]
            path.write_text(json.dumps({"models": models}))
            class Runner:
                host = {"id": directory}
                calls = 0
                def run(self, command, timeout):
                    self.calls += 1
                    return subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
            runner = Runner()
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: agent_models.codex_catalog(rollout, runner), range(24)))
            self.assertEqual(results, [["second", "first"]] * 24)
            self.assertEqual(runner.calls, 1)
            self.assertEqual(agent_models.codex_catalog(rollout), ["second", "first"])
            path.write_text("[]")
            self.assertEqual(agent_models.codex_catalog(rollout), [])
            self.assertEqual(agent_models.preset_for("codex"), [])
            self.assertEqual(agent_models.switch_command("codex"), "/model")
