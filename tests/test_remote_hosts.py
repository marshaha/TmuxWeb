import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from remote_hosts import (FIELD_SEPARATOR, REMOTE_LIST_SCRIPT, TRANSCRIPT_CACHE, RemoteHostStore, SSHRunner,
                          pane_ref, parse_remote_listing, parse_tmux_panes, split_pane_ref, ssh_mux_path,
                          validate_private_key_text)


class RemoteHostTests(unittest.TestCase):
    def test_agent_auth_uses_the_local_keys_without_a_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RemoteHostStore(Path(directory) / "hosts.db", Path(directory) / "key")
            host = store.add_host(name="agent-box", hostname="10.0.0.7", port=22, username="ubuntu",
                                  auth_type="agent", credential="", host_key="10.0.0.7 ssh-ed25519 AAAA",
                                  fingerprint="SHA256:abc")
            stored = store.get_host(host["id"], include_credential=True)
        self.assertEqual(stored["auth_type"], "agent")
        self.assertEqual(stored["credential"], "")
        self.assertEqual(stored["fingerprint"], "SHA256:abc")

    def test_agent_auth_builds_a_non_interactive_publickey_command(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22, "username": "user",
                            "auth_type": "agent", "credential": "", "host_key": "host ssh-ed25519 AAAA"})
        command, environment = runner.command("true", known_hosts="/dev/null", multiplex=False)
        self.assertIn("BatchMode=yes", command)
        # ssh honours only the first value of an option; a leading BatchMode=no
        # made agent mode hang on a password prompt instead of failing fast.
        self.assertNotIn("BatchMode=no", command)
        self.assertIn("PreferredAuthentications=publickey", command)
        self.assertIn("PasswordAuthentication=no", command)
        self.assertNotIn("-i", command)
        self.assertNotIn("IdentitiesOnly=yes", command)
        self.assertEqual(environment, {})

    def test_password_auth_keeps_an_interactive_prompt(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22, "username": "user",
                            "auth_type": "password", "credential": "secret", "host_key": "host ssh-ed25519 AAAA"})
        command, environment = runner.command("true", known_hosts="/dev/null", multiplex=False)
        self.assertIn("BatchMode=no", command)
        self.assertNotIn("BatchMode=yes", command)
        self.assertIn("PreferredAuthentications=password", command)
        self.assertIn("NumberOfPasswordPrompts=1", command)
        self.assertEqual(environment.get("SSH_ASKPASS_REQUIRE"), "force")

    def test_key_auth_disables_prompts(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22, "username": "user",
                            "auth_type": "key", "credential": "PRIVATE", "host_key": "host ssh-ed25519 AAAA"})
        command, _ = runner.command("true", known_hosts="/dev/null", identity_file="/tmp/key", multiplex=False)
        self.assertIn("BatchMode=yes", command)
        self.assertNotIn("BatchMode=no", command)
        self.assertIn("IdentitiesOnly=yes", command)

    def test_database_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "hosts.db"
            RemoteHostStore(db_path, Path(directory) / "key")
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    def test_store_encrypts_credentials_and_round_trips_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RemoteHostStore(root / "hosts.db", root / "master.key")
            host = store.add_host(
                name="build-box", hostname="10.0.0.8", port=22, username="ubuntu",
                auth_type="password", credential="super-secret",
                host_key="example ssh-ed25519 AAAATEST", fingerprint="SHA256:test",
            )
            raw = (root / "hosts.db").read_bytes()
            self.assertNotIn(b"super-secret", raw)
            loaded = store.get_host(host["id"], include_credential=True)
            self.assertEqual(loaded["credential"], "super-secret")
            self.assertEqual(loaded["fingerprint"], "SHA256:test")
            self.assertEqual((root / "master.key").stat().st_mode & 0o777, 0o600)

    def test_tmux_parser_adds_host_scoped_refs(self):
        fields = ["%4", "s:0.1", "s", "0", "main", "1", "claude", "Review", "/work", "1", "120", "40"]
        raw = FIELD_SEPARATOR.join(fields) + "\n"
        host_id = "0123456789abcdef"
        panes = parse_tmux_panes(raw, host_id)
        self.assertEqual(panes[0]["id"], f"{host_id}~%4")
        self.assertEqual(panes[0]["pane_id"], "%4")
        self.assertEqual(panes[0]["window_name"], "main")
        self.assertEqual(split_pane_ref(panes[0]["id"]), (host_id, "%4"))
        self.assertEqual(pane_ref("localhost", "%4"), "%4")

    def test_remote_tmux_format_avoids_control_characters(self):
        # tmux rewrites control characters (and non-ASCII) in -F output when the
        # remote locale is not UTF-8, so the field separator must be printable.
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22,
                            "username": "user", "auth_type": "password", "credential": "x",
                            "host_key": "host ssh-ed25519 AAAA"})
        captured = {}

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(command, **kwargs):
            captured["command"] = command
            return Result()

        runner.run = fake_run
        runner.list_panes()
        self.assertIn(FIELD_SEPARATOR, captured["command"])
        self.assertNotIn("\t", captured["command"])
        self.assertNotIn("\\t", captured["command"])

    def test_ssh_command_multiplexes_and_forces_a_utf8_locale(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host.example", "port": 22,
                            "username": "user", "auth_type": "password", "credential": "x",
                            "host_key": "host ssh-ed25519 AAAA"})
        command, _ = runner.command("tmux list-panes")
        joined = " ".join(command)
        self.assertIn("ControlMaster=auto", joined)
        self.assertIn("ControlPersist=60", joined)
        self.assertIn("ControlPath=", joined)
        self.assertIn("LC_ALL=", command[-1])
        direct, _ = runner.command("tmux list-panes", multiplex=False)
        self.assertNotIn("ControlMaster=auto", " ".join(direct))

    def test_multiplexing_can_be_disabled_by_environment(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host.example", "port": 22,
                            "username": "user", "auth_type": "password", "credential": "x",
                            "host_key": "host ssh-ed25519 AAAA"})
        with mock.patch.dict("os.environ", {"TMUX_WEB_SSH_MUX": "0"}):
            command, _ = runner.command("tmux list-panes")
        self.assertNotIn("ControlMaster=auto", " ".join(command))

    def test_mux_directory_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict("os.environ", {"TMUX_WEB_SSH_MUX_DIR": directory + "/mux"}):
                path = ssh_mux_path({"id": "0123456789abcdef", "hostname": "h", "port": 22, "username": "u"})
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertLess(len(str(path)), 108)

    def test_snapshot_ansi_uses_a_remote_digest_before_transferring_text(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22,
                            "username": "user", "auth_type": "password", "credential": "x",
                            "host_key": "host ssh-ed25519 AAAA"})
        calls = []

        class Result:
            def __init__(self, stdout=b"", returncode=0):
                self.stdout = stdout
                self.stderr = b""
                self.returncode = returncode

        def fake_run(command, **kwargs):
            calls.append(command)
            if command.endswith("| cksum"):
                return Result(b"3141592653 8192\n")
            return Result(b"pane text\n")

        runner.run = fake_run
        digest, content = runner.snapshot_ansi("%9", 500, "3141592653 8192")
        self.assertEqual(digest, "3141592653 8192")
        self.assertIsNone(content)
        self.assertEqual(len(calls), 1)
        digest, content = runner.snapshot_ansi("%9", 500, "1 1")
        self.assertEqual(content, "pane text\n")
        self.assertEqual(len(calls), 3)

    def fake_transcript_runner(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        runner = self.make_runner()

        class Result:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

        runner.run = lambda command, **kwargs: Result()
        return runner

    def test_remote_transcript_pulls_bytes_and_parses_them_here(self):
        payload = "\n".join([
            json.dumps({"type": "session_meta", "payload": {"cwd": "/srv/work"}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "user", "id": "m1",
                "content": [{"type": "input_text", "text": "远端问题"}]}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "m2",
                "content": [{"type": "output_text", "text": "远端回答"}]}}),
        ]) + "\n"
        stdout = b"TMUXWEB_PROVIDER<::>codex\nTMUXWEB_SESSION<::>abc-123\n" \
                 b"TMUXWEB_FORMAT<::>jsonl\nTMUXWEB_PAYLOAD\n" + payload.encode()
        runner = self.fake_transcript_runner(stdout)
        data = runner.transcript("%9", 20)
        self.assertEqual(data["provider"], "codex")
        self.assertEqual(data["session_id"], "abc-123")
        self.assertEqual([(item["role"], item["text"]) for item in data["messages"]],
                         [("user", "远端问题"), ("assistant", "远端回答")])
        self.assertEqual(data["pane"], "%9")

    def test_remote_transcript_needs_no_python_on_the_host(self):
        payload = "\n".join([
            json.dumps({"role": "user", "content": "问题"}),
            json.dumps({"role": "assistant", "content": "回答"}),
        ]) + "\n"
        stdout = b"TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>20261012_1\n" \
                 b"TMUXWEB_FORMAT<::>hermes-jsonl\nTMUXWEB_PAYLOAD\n" + payload.encode()
        runner = self.make_runner()
        captured = {}

        class Result:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = stdout, b"", 0

        def fake_run(command, **kwargs):
            captured["command"] = command
            return Result()

        runner.run = fake_run
        data = runner.transcript("%9", 10)
        command = captured["command"]
        # transcripts are pulled as file bytes (tail) and parsed locally; sqlite3 and
        # a Hermes-only python3 runtime are last-resort paths, never the default
        self.assertIn("tail -c", command)
        self.assertIn("grep -lF", command)
        self.assertLess(command.index("tail -c"), command.index("python3"))
        self.assertNotIn("--pane", command)  # the old python reader invocation is gone
        self.assertEqual([item["text"] for item in data["messages"]], ["问题", "回答"])

    def test_remote_transcript_surfaces_helper_errors(self):
        stdout = b"TMUXWEB_ERROR<::>no Codex transcript for /srv/work\n"
        runner = self.fake_transcript_runner(stdout, returncode=23)
        with self.assertRaisesRegex(RuntimeError, "no Codex transcript"):
            runner.transcript("%9", 10)

    def test_remote_transcript_rejects_empty_sessions(self):
        stdout = b"TMUXWEB_PROVIDER<::>claude\nTMUXWEB_SESSION<::>abc\n" \
                 b"TMUXWEB_FORMAT<::>jsonl\nTMUXWEB_PAYLOAD\n"
        runner = self.fake_transcript_runner(stdout)
        with self.assertRaisesRegex(RuntimeError, "no messages"):
            runner.transcript("%9", 10)

    def make_runner(self):
        TRANSCRIPT_CACHE.clear()
        return SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22,
                          "username": "user", "auth_type": "password", "credential": "x",
                          "host_key": "host ssh-ed25519 AAAA"})

    def test_remote_directory_listing_needs_no_python_on_the_host(self):
        runner = self.make_runner()
        captured = {}

        class Result:
            def __init__(self, stdout=b"", stderr=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

        listing = (
            b"TMUXWEB_PATH\t/srv/work\n"
            b"f\t12\t1793000000.5\t/srv/work/notes.md\n"
            b"d\t4096\t1793000001.0\t/srv/work/src\n"
            b"f\t3\t1793000002.0\t/srv/work/.env\n"
        )

        def fake_run(command, **kwargs):
            captured["command"] = command
            return Result(listing)

        runner.run = fake_run
        data = runner.list_files("/srv/work")
        self.assertNotIn("python3", captured["command"])
        self.assertIn("find -L", captured["command"])
        self.assertEqual(data["path"], "/srv/work")
        self.assertEqual([entry["name"] for entry in data["entries"]], ["src", "notes.md"])
        self.assertEqual(data["entries"][1]["size"], 12)
        self.assertEqual(data["parent"], "/srv")
        self.assertEqual(data["host_id"], "0123456789abcdef")

    def test_remote_listing_parser_handles_gnu_stat_and_bsd_stat_formats(self):
        directory = "/srv/work"
        gnu = ("TMUXWEB_PATH\t/srv/work\n"
               "regular file\t42\t1793000000\t/srv/work/a.sql\n"
               "directory\t4096\t1793000001\t/srv/work/sub\n"
               "symbolic link\t7\t1793000002\t/srv/work/link\n")
        parsed = parse_remote_listing(gnu, directory, show_hidden=True)
        self.assertEqual([(entry["name"], entry["type"]) for entry in parsed["entries"]],
                         [("sub", "dir"), ("a.sql", "file"), ("link", "link")])
        self.assertEqual(parsed["entries"][1]["size"], 42)
        bsd = ("TMUXWEB_PATH\t/srv/work\n"
               "Regular File\t9\t1793000000\t/srv/work/b.sql\n"
               "Directory\t4096\t1793000001\t/srv/work/sub\n")
        parsed = parse_remote_listing(bsd, directory)
        self.assertEqual([(entry["name"], entry["type"]) for entry in parsed["entries"]],
                         [("sub", "dir"), ("b.sql", "file")])

    def test_remote_listing_parser_reports_errors_and_limits(self):
        with self.assertRaises(RuntimeError):
            parse_remote_listing("TMUXWEB_ERROR\tnot a directory: /etc/hosts\n", "/etc/hosts")
        raw = "TMUXWEB_PATH\t/srv\n" + "".join(
            f"f\t1\t1793000000\t/srv/f{index}.txt\n" for index in range(5)
        )
        parsed = parse_remote_listing(raw, "/srv", limit=2)
        self.assertEqual(len(parsed["entries"]), 2)
        self.assertTrue(parsed["truncated"])

    def test_remote_listing_script_runs_with_plain_sh(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "visible.txt").write_text("x" * 7)
            Path(directory, ".hidden").write_text("x")
            Path(directory, "sub").mkdir()
            completed = subprocess.run(["sh", "-c", REMOTE_LIST_SCRIPT, "sh", directory],
                                       capture_output=True, text=True)
            parsed = parse_remote_listing(completed.stdout, directory)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual([(entry["name"], entry["type"], entry["size"]) for entry in parsed["entries"]],
                         [("sub", "dir", 0), ("visible.txt", "file", 7)])

    def test_remote_file_size_limit_is_enforced_before_transfer(self):
        runner = self.make_runner()

        class Result:
            def __init__(self, stdout=b"", stderr=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

        runner.run = lambda command, **kwargs: Result(b"x" * 11)
        with self.assertRaisesRegex(RuntimeError, "download limit"):
            runner.download_file("/srv/work/big.bin", 10)
        self.assertEqual(runner.download_file("/srv/work/small.bin", 100), b"x" * 11)

    def test_remote_upload_streams_bytes_and_creates_parent(self):
        runner = self.make_runner()
        captured = {}

        class Result:
            stdout = b""
            stderr = b""
            returncode = 0

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input_bytes")
            return Result()

        runner.run = fake_run
        runner.upload_file("/srv/work/sub/file.bin", b"\x00binary\xff")
        self.assertIn("mkdir -p", captured["command"])
        self.assertIn("cat >", captured["command"])
        self.assertEqual(captured["input"], b"\x00binary\xff")

    def transcript_runner(self, header_extra: str, payload_text: str):
        """Fake runner that serves a transcript response and counts SSH calls."""
        calls = []

        class Result:
            def __init__(self, stdout=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, b"", returncode

        body = ("TMUXWEB_PROVIDER<::>claude\nTMUXWEB_SESSION<::>sess-1\n"
                "TMUXWEB_FORMAT<::>jsonl\n" + header_extra + "TMUXWEB_PAYLOAD\n" + payload_text).encode()

        def fake_run(command, **kwargs):
            calls.append(command)
            if command.startswith("stat "):
                return Result(b"1793000100 4096\n")
            return Result(body)

        runner = self.make_runner()
        runner.run = fake_run
        return runner, calls

    def test_transcript_carries_the_file_mtime(self):
        payload = json.dumps({"type": "user", "uuid": "u1",
                              "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n"
        runner, _calls = self.transcript_runner(
            "TMUXWEB_PRIMARY<::>/srv/work/sess-1.jsonl\nTMUXWEB_STAMP<::>1793000100 4096\n", payload)
        data = runner.transcript("%9", 20)
        self.assertEqual(data["mtime"], 1793000100.0)
        self.assertEqual([item["text"] for item in data["messages"]], ["hi"])

    def test_repeated_polls_hit_the_cache_then_a_single_stat(self):
        payload = json.dumps({"type": "user", "uuid": "u1",
                              "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n"
        runner, calls = self.transcript_runner(
            "TMUXWEB_PRIMARY<::>/srv/work/sess-1.jsonl\nTMUXWEB_STAMP<::>1793000100 4096\n", payload)
        first = runner.transcript("%9", 20)
        self.assertEqual(len(calls), 1)
        second = runner.transcript("%9", 20)          # inside the TTL: no SSH at all
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["messages"], second["messages"])
        key = next(iter(TRANSCRIPT_CACHE))
        TRANSCRIPT_CACHE[key]["at"] = 0                # expire the entry
        third = runner.transcript("%9", 20)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[-1].startswith("stat "))
        self.assertEqual(third["messages"], first["messages"])

    def test_changed_stamp_triggers_a_refetch(self):
        payload = json.dumps({"type": "user", "uuid": "u1",
                              "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n"
        runner, calls = self.transcript_runner(
            "TMUXWEB_PRIMARY<::>/srv/work/sess-1.jsonl\nTMUXWEB_STAMP<::>1793000100 4096\n", payload)
        runner.transcript("%9", 20)
        key = next(iter(TRANSCRIPT_CACHE))
        TRANSCRIPT_CACHE[key]["at"] = 0

        class Result:
            def __init__(self, stdout=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, b"", returncode

        def fake_run(command, **kwargs):
            calls.append(command)
            if command.startswith("stat "):
                return Result(b"1793000200 8192\n")  # the file grew
            return Result(("TMUXWEB_PROVIDER<::>claude\nTMUXWEB_SESSION<::>sess-1\n"
                           "TMUXWEB_FORMAT<::>jsonl\nTMUXWEB_PRIMARY<::>/srv/work/sess-1.jsonl\n"
                           "TMUXWEB_STAMP<::>1793000200 8192\nTMUXWEB_PAYLOAD\n" + payload).encode())

        runner.run = fake_run
        data = runner.transcript("%9", 20)
        self.assertEqual(len(calls), 3)  # stat + full fetch
        self.assertTrue(calls[-1].startswith("sh -c"))
        self.assertEqual(data["mtime"], 1793000200.0)

    def test_hermes_rows_also_probe_the_write_ahead_log(self):
        rows = json.dumps([{"id": 2, "role": "assistant", "content": "回答", "timestamp": "t"}])
        runner = self.make_runner()

        class Result:
            def __init__(self, stdout=b"", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, b"", returncode

        body = ("TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s1\nTMUXWEB_FORMAT<::>hermes-rows\n"
                "TMUXWEB_PRIMARY<::>/home/u/.hermes/state.db\n"
                "TMUXWEB_STAMP<::>1793000100 4096\nTMUXWEB_PAYLOAD\n" + rows).encode()
        probed = []

        def fake_run(command, **kwargs):
            probed.append(command)
            return Result(b"1793000100 4096\n" if command.startswith("stat ") else body)

        runner.run = fake_run
        runner.transcript("%9", 20)
        key = next(iter(TRANSCRIPT_CACHE))
        self.assertEqual(TRANSCRIPT_CACHE[key]["probe"],
                         ["/home/u/.hermes/state.db", "/home/u/.hermes/state.db-wal"])
        TRANSCRIPT_CACHE[key]["at"] = 0
        runner.transcript("%9", 20)
        self.assertIn("state.db-wal", probed[-1])

    def test_invalid_pane_refs_are_rejected(self):
        for value in ["host~bad", "../%4", "host/name~%4", "host~%x"]:
            with self.assertRaises(ValueError):
                split_pane_ref(value)

    def test_password_is_not_present_in_ssh_arguments_or_environment(self):
        runner = SSHRunner({
            "hostname": "example.test", "port": 2222, "username": "alice",
            "auth_type": "password", "credential": "top-secret",
            "host_key": "example.test ssh-ed25519 AAAA", "id": "0123456789abcdef",
        })
        command, environment = runner.command("tmux list-panes")
        self.assertNotIn("top-secret", " ".join(command))
        self.assertNotIn("top-secret", " ".join(environment.values()))
        self.assertEqual(environment["SSH_ASKPASS_REQUIRE"], "force")

    def test_public_key_is_rejected_as_private_key(self):
        with self.assertRaisesRegex(ValueError, "public key"):
            validate_private_key_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest user@example")


if __name__ == "__main__":
    unittest.main()
