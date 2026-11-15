"""Per-pane restart-and-resume: rebuild the agent argv with a resume flag,
refuse while the agent is busy, and verify the new process really resumed
the same session."""

import time
import unittest

import pane_restart
from pane_restart import RestartResumeError, resume_argv


class ResumeArgvTests(unittest.TestCase):
    def test_claude_bare_gets_resume_appended(self):
        self.assertEqual(
            resume_argv("claude", ["claude"], "11111111-2222-3333-4444-555555555555"),
            ["claude", "--resume", "11111111-2222-3333-4444-555555555555"])

    def test_claude_keeps_other_flags_and_appends_resume(self):
        self.assertEqual(
            resume_argv("claude", ["/opt/homebrew/bin/claude", "--model", "opus"],
                        "11111111-2222-3333-4444-555555555555"),
            ["/opt/homebrew/bin/claude", "--model", "opus",
             "--resume", "11111111-2222-3333-4444-555555555555"])

    def test_claude_replaces_an_existing_resume_id(self):
        self.assertEqual(
            resume_argv("claude",
                        ["claude", "--resume", "aaaaaaaa-1111-2222-3333-444444444444"],
                        "11111111-2222-3333-4444-555555555555"),
            ["claude", "--resume", "11111111-2222-3333-4444-555555555555"])

    def test_claude_replaces_resume_equals_form(self):
        self.assertEqual(
            resume_argv("claude", ["claude", "--resume=aaaaaaaa-1111-2222-3333-444444444444"],
                        "11111111-2222-3333-4444-555555555555"),
            ["claude", "--resume=11111111-2222-3333-4444-555555555555"])

    def test_claude_replaces_short_r_flag(self):
        self.assertEqual(
            resume_argv("claude", ["claude", "-r", "aaaaaaaa-1111-2222-3333-444444444444"],
                        "11111111-2222-3333-4444-555555555555"),
            ["claude", "-r", "11111111-2222-3333-4444-555555555555"])

    def test_claude_session_id_flag_becomes_resume(self):
        # --session-id starts a *new* session with that id; resuming needs --resume.
        self.assertEqual(
            resume_argv("claude", ["claude", "--session-id",
                                   "aaaaaaaa-1111-2222-3333-444444444444"],
                        "11111111-2222-3333-4444-555555555555"),
            ["claude", "--resume", "11111111-2222-3333-4444-555555555555"])

    def test_claude_resume_without_a_value_gets_one(self):
        self.assertEqual(
            resume_argv("claude", ["claude", "--resume"],
                        "11111111-2222-3333-4444-555555555555"),
            ["claude", "--resume", "11111111-2222-3333-4444-555555555555"])

    def test_hermes_and_kimi_use_the_same_flag(self):
        self.assertEqual(
            resume_argv("hermes", ["hermes"], "session_11111111-2222-3333-4444-555555555555"),
            ["hermes", "--resume", "session_11111111-2222-3333-4444-555555555555"])
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--yolo"], "session_11111111-2222-3333-4444-555555555555"),
            ["kimi", "--yolo", "--resume", "session_11111111-2222-3333-4444-555555555555"])

    def test_kimi_keeps_its_session_flag_and_replaces_the_id(self):
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--session", "session_aaaaaaaa-1111-2222-3333-444444444444"],
                        "session_11111111-2222-3333-4444-555555555555"),
            ["kimi", "--session", "session_11111111-2222-3333-4444-555555555555"])

    def test_kimi_short_session_flag_replaces_the_id(self):
        self.assertEqual(
            resume_argv("kimi", ["kimi", "-S", "session_aaaaaaaa-1111-2222-3333-444444444444"],
                        "session_11111111-2222-3333-4444-555555555555"),
            ["kimi", "-S", "session_11111111-2222-3333-4444-555555555555"])

    def test_kimi_creation_flags_are_stripped_before_resume(self):
        # --agent/--agent-file/--environment/--environment-cwd only apply to
        # a new session; kept, the respawn would be rejected at startup
        new = "session_11111111-2222-3333-4444-555555555555"
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--agent", "reviewer"], new),
            ["kimi", "--resume", new])
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--agent=reviewer"], new),
            ["kimi", "--resume", new])
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--agent-file", "/tmp/reviewer.md", "--model", "x"], new),
            ["kimi", "--model", "x", "--resume", new])
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--environment", "dev", "--environment-cwd", "/srv"], new),
            ["kimi", "--resume", new])
        self.assertEqual(
            resume_argv("kimi", ["kimi", "--agent", "reviewer", "--resume",
                                 "session_aaaaaaaa-1111-2222-3333-444444444444"], new),
            ["kimi", "--resume", new])

    def test_kimi_continue_is_stripped_before_resume(self):
        # --continue/-c means "resume the latest" and is mutually exclusive
        # with --resume; the id wins and the flag goes
        new = "session_11111111-2222-3333-4444-555555555555"
        self.assertEqual(resume_argv("kimi", ["kimi", "--continue"], new),
                         ["kimi", "--resume", new])
        self.assertEqual(resume_argv("kimi", ["kimi", "-c"], new),
                         ["kimi", "--resume", new])

    def test_creation_flag_stripping_is_kimi_only(self):
        # other providers keep their argv untouched: the rule is kimi's
        original = ["claude", "--agent", "reviewer"]
        self.assertEqual(
            resume_argv("claude", original, "11111111-2222-3333-4444-555555555555"),
            ["claude", "--agent", "reviewer", "--resume", "11111111-2222-3333-4444-555555555555"])
        self.assertEqual(original, ["claude", "--agent", "reviewer"])

    def test_codex_bare_gets_the_resume_subcommand(self):
        self.assertEqual(
            resume_argv("codex", ["codex"], "01911111111111111111111111"),
            ["codex", "resume", "01911111111111111111111111"])

    def test_codex_replaces_the_resume_subcommand_id(self):
        self.assertEqual(
            resume_argv("codex", ["codex", "resume", "01900000000000000000000000"],
                        "01911111111111111111111111"),
            ["codex", "resume", "01911111111111111111111111"])

    def test_codex_resume_after_global_flags_is_found(self):
        self.assertEqual(
            resume_argv("codex", ["codex", "--model", "gpt-5", "resume",
                                  "01900000000000000000000000"],
                        "01911111111111111111111111"),
            ["codex", "--model", "gpt-5", "resume", "01911111111111111111111111"])

    def test_codex_node_wrapper_inserts_after_the_script(self):
        self.assertEqual(
            resume_argv("codex", ["node", "/opt/codex/bin/codex.js", "--model", "gpt-5"],
                        "01911111111111111111111111"),
            ["node", "/opt/codex/bin/codex.js", "resume", "01911111111111111111111111",
             "--model", "gpt-5"])

    def test_input_argv_is_not_mutated(self):
        argv = ["claude", "--model", "opus"]
        resume_argv("claude", argv, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(argv, ["claude", "--model", "opus"])

    def test_unsupported_provider_is_refused(self):
        for provider in ("opencode", "gemini", "bash", ""):
            with self.assertRaises(ValueError):
                resume_argv(provider, ["x"], "11111111-2222-3333-4444-555555555555")

    def test_bad_session_id_is_refused(self):
        for bad in ("", "short", "has space", "a" * 65, "bad/id", "123"):
            with self.assertRaises(ValueError):
                resume_argv("claude", ["claude"], bad)

    def test_empty_argv_is_refused(self):
        with self.assertRaises(ValueError):
            resume_argv("claude", [], "11111111-2222-3333-4444-555555555555")


class HermesIdleTests(unittest.TestCase):
    def test_quiet_assistant_tail_is_idle(self):
        data = {"messages": [{"role": "assistant", "text": "done"}],
                "mtime": (time.time() - 60) * 1e9}  # local reader reports nanoseconds
        self.assertTrue(pane_restart.hermes_idle(data, quiet_seconds=30))

    def test_recent_write_is_not_idle(self):
        data = {"messages": [{"role": "assistant", "text": "done"}],
                "mtime": time.time() - 5}  # remote reader reports seconds
        self.assertFalse(pane_restart.hermes_idle(data, quiet_seconds=30))

    def test_trailing_user_message_is_not_idle(self):
        data = {"messages": [{"role": "user", "text": "go"}],
                "mtime": (time.time() - 3600) * 1e9}
        self.assertFalse(pane_restart.hermes_idle(data, quiet_seconds=30))

    def test_no_messages_is_not_idle(self):
        self.assertFalse(pane_restart.hermes_idle({"messages": [], "mtime": 0}))


class VerifyTests(unittest.TestCase):
    def test_verified_when_pid_hint_and_session_all_match(self):
        calls = {"n": 0}

        def info():
            calls["n"] += 1
            return {"pane_pid": "200", "session_id": "abc12345"}

        def session():
            return "abc12345"

        ok, detail = pane_restart.verify_resumed(
            info, session, "abc12345", "100", timeout=5, poll=0)
        self.assertTrue(ok)
        self.assertIn("pid", detail)

    def test_pid_must_change(self):
        def info():
            return {"pane_pid": "100", "session_id": "abc12345"}

        ok, _ = pane_restart.verify_resumed(
            info, lambda: "abc12345", "abc12345", "100", timeout=1, poll=0)
        self.assertFalse(ok)

    def test_hint_must_match_the_old_session(self):
        def info():
            return {"pane_pid": "200", "session_id": "different1"}

        ok, _ = pane_restart.verify_resumed(
            info, lambda: "abc12345", "abc12345", "100", timeout=1, poll=0)
        self.assertFalse(ok)

    def test_transcript_mismatch_fails_verification(self):
        def info():
            return {"pane_pid": "200", "session_id": "abc12345"}

        ok, detail = pane_restart.verify_resumed(
            info, lambda: "other123", "abc12345", "100", timeout=1, poll=0)
        self.assertFalse(ok)
        self.assertIn("transcript", detail)

    def test_unreadable_transcript_still_verifies_on_pid_and_hint(self):
        # a session with no messages yet has no readable transcript; the new
        # pid plus the resume flag in its argv is the verification then
        def info():
            return {"pane_pid": "200", "session_id": "abc12345"}

        ok, detail = pane_restart.verify_resumed(
            info, lambda: None, "abc12345", "100", timeout=1, poll=0)
        self.assertTrue(ok)
        self.assertIn("transcript", detail)


class FakeTransport:
    """Scripted stand-in for TmuxBackend / SSHRunner."""

    def __init__(self, transcript_data, status="idle", info=None):
        self.transcript_data = transcript_data
        self.status = status
        self.info = info or {"provider": "claude", "session_id": "abc12345",
                             "argv": ["claude"], "pane_pid": "100"}
        self.respawned = []
        self.transcript_calls = 0

    def transcript(self, pane_id, limit=200, refresh=False, pane=None):
        self.transcript_calls += 1
        if isinstance(self.transcript_data, Exception):
            raise self.transcript_data
        return dict(self.transcript_data)

    def status_many(self, pane_ids):
        return {pane: self.status for pane in pane_ids}

    def restart_info(self, pane_id, pane=None):
        return dict(self.info)

    def restart_resume(self, pane_id, argv, pane=None, term_wait=None):
        self.respawned.append(list(argv))
        # the new process replaces the old one, with the resume flag
        self.info = dict(self.info, pane_pid="200")
        return {"pane": pane_id, "command": " ".join(argv)}


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.pane = {"id": "%0", "host_id": "localhost", "path": "/tmp",
                     "title": "t", "command": "claude"}

    def run_flow(self, transport, **kwargs):
        kwargs.setdefault("idle_check", lambda *args: "idle")
        kwargs.setdefault("verify_timeout", 5)
        kwargs.setdefault("verify_poll", 0)
        return pane_restart.restart_and_resume(
            transport, "%0", self.pane, "claude", **kwargs)

    def test_happy_path_resumes_and_verifies(self):
        transport = FakeTransport(
            {"provider": "claude", "session_id": "abc12345",
             "messages": [{"role": "assistant", "text": "hi"}], "mtime": 0})
        result = self.run_flow(transport)
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(result["session_id"], "abc12345")
        self.assertTrue(result["verified"])
        self.assertEqual(transport.respawned,
                         [["claude", "--resume", "abc12345"]])

    def test_busy_pane_is_refused_and_never_respawned(self):
        transport = FakeTransport(
            {"provider": "claude", "session_id": "abc12345",
             "messages": [{"role": "assistant", "text": "hi"}]})
        with self.assertRaises(RestartResumeError):
            self.run_flow(transport, idle_check=lambda *args: "busy")
        self.assertEqual(transport.respawned, [])

    def test_unknown_status_is_refused(self):
        transport = FakeTransport(
            {"provider": "claude", "session_id": "abc12345",
             "messages": [{"role": "assistant", "text": "hi"}]})
        with self.assertRaises(RestartResumeError):
            self.run_flow(transport, idle_check=lambda *args: "unknown")
        self.assertEqual(transport.respawned, [])

    def test_no_session_is_refused_and_never_respawned(self):
        transport = FakeTransport(
            RuntimeError("no Claude transcript for /tmp"),
            info={"provider": "claude", "session_id": "", "argv": ["claude"],
                  "pane_pid": "100"})
        with self.assertRaises(RestartResumeError):
            self.run_flow(transport)
        self.assertEqual(transport.respawned, [])

    def test_hint_covers_a_transcript_without_messages(self):
        # a pane resumed from a session that has no messages yet: the
        # transcript lookup fails, but the argv hint still names the session
        transport = FakeTransport(RuntimeError("this session has no messages yet"))
        result = self.run_flow(transport)
        self.assertEqual(result["session_id"], "abc12345")
        self.assertEqual(transport.respawned,
                         [["claude", "--resume", "abc12345"]])

    def test_no_agent_argv_is_refused(self):
        transport = FakeTransport(
            {"provider": "claude", "session_id": "abc12345",
             "messages": [{"role": "assistant", "text": "hi"}]},
            info={"provider": "claude", "session_id": "", "argv": [],
                  "pane_pid": "100"})
        with self.assertRaises(RestartResumeError):
            self.run_flow(transport)
        self.assertEqual(transport.respawned, [])

    def test_verification_failure_is_reported(self):
        transport = FakeTransport(
            {"provider": "claude", "session_id": "abc12345",
             "messages": [{"role": "assistant", "text": "hi"}]})
        # the respawned process never shows the resume hint: verification fails
        transport.info = {"provider": "claude", "session_id": "",
                          "argv": ["claude"], "pane_pid": "200"}
        with self.assertRaises(RestartResumeError):
            self.run_flow(transport, verify_timeout=1)
        self.assertEqual(len(transport.respawned), 1)


if __name__ == "__main__":
    unittest.main()
