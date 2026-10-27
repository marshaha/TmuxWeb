import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import transcript_reader


class ClassifyCommandTests(unittest.TestCase):
    def test_known_agent_commands_map_to_providers(self):
        for command, provider in [
            ("claude", "claude"), ("codex", "codex"), ("kimi", "kimi"),
            ("python", "hermes"), ("python3", "hermes"), ("hermes", "hermes"),
        ]:
            self.assertEqual(transcript_reader.provider_for_command(command), provider, command)

    def test_non_agent_commands_have_no_provider(self):
        for command in ("zsh", "bash", "vim", "./myrem", "sudo", "", None):
            self.assertIsNone(transcript_reader.provider_for_command(command), command)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_claude(self, cwd="/work/repo", session="11111111-2222-3333-4444-555555555555"):
        project = self.home / ".claude" / "projects" / cwd.replace("/", "-")
        project.mkdir(parents=True, exist_ok=True)
        events = [
            {"type": "user", "uuid": "u1", "message": {"content": [{"type": "text", "text": "问题"}]}},
            {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "回答"}]}},
            {"type": "user", "uuid": "u2", "isCompactSummary": True,
             "message": {"content": [{"type": "text", "text": "compacted"}]}},
            {"type": "user", "uuid": "u3", "message": {"content": [{"type": "text", "text": "<local-command-x>"}]}},
        ]
        path = project / f"{session}.jsonl"
        path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
        return path

    def test_claude_transcript_uses_title_match_and_skips_compact_and_notices(self):
        self.write_claude()
        result = transcript_reader.read_transcript("claude", "/work/repo", "问题", 20, home=self.home)
        self.assertEqual(result["provider"], "claude")
        self.assertEqual([(item["role"], item["text"]) for item in result["messages"]],
                         [("user", "问题"), ("assistant", "回答")])
        self.assertGreater(result["mtime"], 0)

    def test_claude_transcript_prefers_resume_hint_over_title(self):
        self.write_claude()
        hint = "99999999-8888-7777-6666-555555555555"
        self.write_claude(session=hint)
        result = transcript_reader.read_transcript("claude", "/work/repo", "unrelated", 5,
                                                  home=self.home, hint=hint)
        self.assertEqual(result["session_id"], hint)

    def test_claude_transcript_uses_screen_probe_when_title_is_ambiguous(self):
        path = self.write_claude()
        self.write_claude(session="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        probe = "this line only appears in the first session buffer"
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "user", "uuid": "u9",
                                     "message": {"content": [{"type": "text", "text": probe}]}}) + "\n")
        result = transcript_reader.read_transcript(
            "claude", "/work/repo", "", 5, home=self.home, screen=lambda: "header\n" + probe + "\n",
        )
        self.assertEqual(result["session_id"], path.stem)

    def test_codex_transcript_from_session_index(self):
        sessions = self.home / ".codex" / "sessions" / "2026" / "01"
        sessions.mkdir(parents=True)
        session_id = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
        path = sessions / f"rollout-{session_id}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": session_id, "cwd": "/work/repo"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "id": "m1",
                                                  "content": [{"type": "input_text", "text": "问题"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "id": "m2",
                                                  "content": [{"type": "output_text", "text": "回答"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "id": "m2",
                                                  "content": [{"type": "output_text", "text": "重复"}]}},
        ]
        path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records))
        (self.home / ".codex" / "session_index.jsonl").write_text(
            json.dumps({"id": session_id, "thread_name": "Review"}) + "\n"
        )
        result = transcript_reader.read_transcript("codex", "/other", "Review | repo", 10, home=self.home)
        self.assertEqual(result["session_id"], session_id)
        self.assertEqual([item["id"] for item in result["messages"]], ["m1", "m2"])

    def test_hermes_transcript_reads_read_only_database(self):
        hermes = self.home / ".hermes"
        hermes.mkdir()
        connection = sqlite3.connect(hermes / "state.db")
        with connection:
            connection.execute("CREATE TABLE sessions (id TEXT, source TEXT, cwd TEXT, "
                               "started_at REAL, last_activity_at REAL)")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                               "role TEXT, content TEXT, timestamp TEXT, active INTEGER)")
            connection.execute("INSERT INTO sessions VALUES ('s1','cli','/work/repo',1,2)")
            connection.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?)", [
                (1, "s1", "user", "问题", "t1", 1),
                (2, "s1", "assistant", "", "t2", 1),
                (3, "s1", "assistant", "旧回答", "t3", 0),
                (4, "s1", "assistant", "回答", "t4", 1),
            ])
        connection.close()
        result = transcript_reader.read_transcript("hermes", "/work/repo", "", 10, home=self.home)
        self.assertEqual([item["text"] for item in result["messages"]], ["问题", "回答"])

    def test_hermes_prefers_the_session_that_has_messages(self):
        """A fresh empty session row must not shadow the live conversation."""
        hermes = self.home / ".hermes"
        hermes.mkdir()
        connection = sqlite3.connect(hermes / "state.db")
        with connection:
            connection.execute("CREATE TABLE sessions (id TEXT, source TEXT, cwd TEXT, "
                               "started_at REAL, last_activity_at REAL)")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                               "role TEXT, content TEXT, timestamp TEXT, active INTEGER)")
            connection.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
                ("local-abc", "local", "/work/repo", 100, 200),
                ("local-empty", "cli", "/work/repo", 300, None),
            ])
            connection.execute("INSERT INTO messages VALUES (1,'local-abc','user','真实对话','t1',1)")
            connection.execute("INSERT INTO messages VALUES (2,'local-abc','assistant','真实回答','t2',1)")
        connection.close()
        result = transcript_reader.read_transcript("hermes", "/work/repo", "", 10, home=self.home)
        self.assertEqual(result["session_id"], "local-abc")
        self.assertEqual([item["text"] for item in result["messages"]], ["真实对话", "真实回答"])

    def test_hermes_uses_the_resumed_session_hint(self):
        hermes = self.home / ".hermes"
        hermes.mkdir()
        connection = sqlite3.connect(hermes / "state.db")
        with connection:
            connection.execute("CREATE TABLE sessions (id TEXT, source TEXT, cwd TEXT, "
                               "started_at REAL, last_activity_at REAL)")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                               "role TEXT, content TEXT, timestamp TEXT, active INTEGER)")
            connection.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
                ("20261012_234331_aaaaaa", "cli", "/work/repo", 100, 100),
                ("20261013_000000_bbbbbb", "cli", "/work/repo", 200, 200),
            ])
            for index, session_id in enumerate(["20261012_234331_aaaaaa", "20261013_000000_bbbbbb"], 1):
                connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,1)",
                                   (index, session_id, "user", session_id, "t"))
        connection.close()
        result = transcript_reader.read_transcript("hermes", "/work/repo", "", 10, home=self.home,
                                                  hint="20261012_234331_aaaaaa")
        self.assertEqual(result["session_id"], "20261012_234331_aaaaaa")
        newest = transcript_reader.read_transcript("hermes", "/work/repo", "", 10, home=self.home)
        self.assertEqual(newest["session_id"], "20261013_000000_bbbbbb")

    def test_empty_session_is_reported_instead_of_returning_blank(self):
        hermes = self.home / ".hermes"
        hermes.mkdir()
        connection = sqlite3.connect(hermes / "state.db")
        with connection:
            connection.execute("CREATE TABLE sessions (id TEXT, source TEXT, cwd TEXT, "
                               "started_at REAL, last_activity_at REAL)")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                               "role TEXT, content TEXT, timestamp TEXT, active INTEGER)")
            connection.execute("INSERT INTO sessions VALUES ('empty','cli','/work/repo',1,1)")
        connection.close()
        with self.assertRaises(transcript_reader.TranscriptError):
            transcript_reader.read_transcript("hermes", "/work/repo", "", 10, home=self.home)

    def test_resume_hint_accepts_hermes_style_session_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "4242").mkdir()
            (proc / "4242" / "cmdline").write_bytes(
                b"/venv/bin/python\x00/hermes/agent\x00hermes\x00--resume\x0020261012_234331_657c95\x00")
            hint = transcript_reader.resume_session_hint(4242, proc_root=proc)
        self.assertEqual(hint, "20261012_234331_657c95")

    def test_resume_hint_ignores_short_flags_and_walks_children(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid in ("10", "11", "12"):
                (proc / pid).mkdir()
            (proc / "10" / "cmdline").write_bytes(b"claude\x00-r\x0099999999-8888-7777-6666-555555555555\x00")
            (proc / "11" / "cmdline").write_bytes(b"node\x00server.js\x00")
            hint = transcript_reader.resume_session_hint(10, proc_root=proc)
        self.assertEqual(hint, "99999999-8888-7777-6666-555555555555")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(transcript_reader.TranscriptError):
            transcript_reader.read_transcript(None, "/work", "", 10, home=self.home)


class ReaderCliTests(unittest.TestCase):
    def test_main_prints_json_payload_for_an_unsupported_command(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = transcript_reader.main(["--command", "zsh", "--cwd", "/work", "--limit", "5"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("not supported", payload["error"])


if __name__ == "__main__":
    unittest.main()
