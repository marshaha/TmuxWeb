import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

import relay


def relay_status(text):
    return relay.classify(text)
from pathlib import Path

import remote_hosts

from remote_hosts import (FIELD_SEPARATOR, REMOTE_LIST_SCRIPT, REMOTE_TRANSCRIPT_TAIL_SCRIPT, TRANSCRIPT_CACHE,
                          TRANSCRIPT_STATE, RemoteHostStore, SSHRunner, pane_ref, parse_remote_listing,
                          parse_tmux_panes, parse_transcript_response, split_pane_ref, ssh_mux_path,
                          validate_private_key_text)


FAKE_SSH_HOST = {"id": "probe", "hostname": "10.0.0.9", "port": 22, "username": "ubuntu",
                 "auth_type": "agent", "host_key": "10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbc"}


def clear_tmux_cache(host):
    key = remote_hosts.tmux_cache_key(host)
    remote_hosts._TMUX_BIN_CACHE.pop(key, None)
    remote_hosts._TMUX_BIN_FAILED.pop(key, None)


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

    def test_jump_hosts_are_validated_and_cleared_on_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RemoteHostStore(root / "hosts.db", root / "master.key")
            bastion = store.add_host(name="bastion", hostname="10.0.0.1", port=22, username="jump",
                                     auth_type="agent", credential="", host_key="", fingerprint="")
            target = store.add_host(name="target", hostname="10.0.0.2", port=22, username="app",
                                    auth_type="agent", credential="", host_key="", fingerprint="",
                                    jump_host_id=bastion["id"])
            self.assertEqual(target["jump_host_id"], bastion["id"])
            self.assertEqual(store.jump_host(target)["name"], "bastion")
            self.assertIsNone(store.jump_host(store.get_host(bastion["id"])))

            # a host cannot jump through itself, through an unknown id, or in a loop
            with self.assertRaises(ValueError):
                store.update_host(bastion["id"], jump_host_id=bastion["id"])
            with self.assertRaises(ValueError):
                store.update_host(bastion["id"], jump_host_id="deadbeefdeadbeef")
            with self.assertRaises(ValueError):
                store.update_host(bastion["id"], jump_host_id=target["id"])

            # editing without touching the hop keeps it
            store.update_host(target["id"], name="target-renamed")
            self.assertEqual(store.get_host(target["id"])["jump_host_id"], bastion["id"])

            # deleting the bastion falls back to a direct connection
            store.delete_host(bastion["id"])
            self.assertEqual(store.get_host(target["id"])["jump_host_id"], "")

    def test_a_jump_host_becomes_a_proxy_command(self):
        jump = {"id": "aaaa", "hostname": "bastion.example", "port": 2222, "username": "hop",
                "auth_type": "agent", "credential": "", "host_key": "bastion ssh-ed25519 AAAA"}
        runner = SSHRunner({"id": "bbbb", "hostname": "target.example", "port": 22, "username": "app",
                            "auth_type": "key", "credential": "PRIVATE", "host_key": "target ssh-ed25519 BBBB",
                            "jump": jump})
        with tempfile.TemporaryDirectory() as directory:
            proxy, _ = runner.build_jump_proxy(Path(directory))
        command, _ = runner.command("true", known_hosts="/dev/null", identity_file="/tmp/key",
                                    multiplex=False, jump_proxy=proxy)
        proxy = next(item for item in command if item.startswith("ProxyCommand="))
        self.assertIn("bastion.example", proxy)
        self.assertIn("-p 2222", proxy)
        self.assertIn("-W %h:%p", proxy)
        self.assertIn("hop@bastion.example", proxy)
        # the hop is pinned like any other host
        self.assertIn("StrictHostKeyChecking=yes", proxy)
        self.assertEqual(runner.jump_description(), "hop@bastion.example:2222")

    def test_a_jump_chain_is_resolved_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RemoteHostStore(root / "hosts.db", root / "master.key")
            outer = store.add_host(name="outer", hostname="10.0.0.1", port=22, username="hop",
                                   auth_type="agent", credential="", host_key="", fingerprint="")
            middle = store.add_host(name="middle", hostname="10.0.0.2", port=22, username="hop",
                                    auth_type="agent", credential="", host_key="", fingerprint="",
                                    jump_host_id=outer["id"])
            inner = store.add_host(name="inner", hostname="10.0.0.3", port=22, username="app",
                                   auth_type="agent", credential="", host_key="", fingerprint="",
                                   jump_host_id=middle["id"])
            self.assertEqual([hop["name"] for hop in store.jump_chain(store.get_host(inner["id"]))],
                             ["middle", "outer"])
            self.assertEqual(store.jump_chain(store.get_host(inner["id"]), limit=1)[0]["name"], "middle")
            # closing the loop is rejected
            with self.assertRaises(ValueError):
                store.update_host(outer["id"], jump_host_id=inner["id"])

    def test_two_hops_become_nested_proxy_commands(self):
        middle = {"id": "bbbb", "hostname": "mid.example", "port": 2201, "username": "hop",
                  "auth_type": "key", "credential": "PRIVATE-B", "host_key": ""}
        outer = {"id": "cccc", "hostname": "outer.example", "port": 2202, "username": "hop",
                 "auth_type": "key", "credential": "PRIVATE-C", "host_key": ""}
        runner = SSHRunner({"id": "dddd", "hostname": "target.example", "port": 22, "username": "app",
                            "auth_type": "agent", "credential": "", "host_key": "",
                            "jump_chain": [middle, outer]})
        with tempfile.TemporaryDirectory() as directory:
            proxy, passwords = runner.build_jump_proxy(Path(directory))
        self.assertIn("mid.example", proxy)
        self.assertIn("outer.example", proxy)
        # the outer hop is dialled by the middle one: it sits inside the nested
        # ProxyCommand, which the middle ssh runs before connecting itself
        nested_at = proxy.index("ProxyCommand=")
        self.assertLess(proxy.index("-p 2201"), nested_at)          # middle port in the outer options
        self.assertGreater(proxy.index("outer.example"), nested_at)  # outer hop is nested
        self.assertGreater(proxy.index("hop@mid.example"), nested_at)  # its own destination comes last
        self.assertGreater(proxy.index("-p 2202"), nested_at)
        self.assertEqual(passwords, [])
        self.assertEqual(runner.jump_description(), "hop@mid.example:2201 -> hop@outer.example:2202")

    def test_a_password_hop_and_a_password_target_share_one_askpass(self):
        hop = {"id": "eeee", "hostname": "hop.example", "port": 22, "username": "hop",
               "auth_type": "password", "credential": "hop-secret", "host_key": ""}
        runner = SSHRunner({"id": "ffff", "hostname": "target.example", "port": 22, "username": "app",
                            "auth_type": "password", "credential": "target-secret", "host_key": "",
                            "jump_chain": [hop]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy, passwords = runner.build_jump_proxy(root)
            target_password = root / "password"
            target_password.write_text("target-secret")
            askpass = root / "askpass.sh"
            runner._write_askpass(root, target_password, passwords, askpass)
            body = askpass.read_text()
            hop_secret_file = passwords[0][1].read_text()
        self.assertEqual(len(passwords), 1)
        self.assertEqual(hop_secret_file, "hop-secret")
        self.assertIn("hop.example", body)
        # the target's own password is the fallback branch
        self.assertIn(str(target_password), body)
        self.assertTrue(body.startswith("#!/bin/sh"))

    def test_a_jump_host_can_be_typed_instead_of_saved(self):
        self.assertEqual(RemoteHostStore.parse_jump_spec("bastion.example", default_user="app"),
                         {"hostname": "bastion.example", "port": 22, "username": "app"})
        self.assertEqual(RemoteHostStore.parse_jump_spec("ops@bastion.example:2222"),
                         {"hostname": "bastion.example", "port": 2222, "username": "ops"})
        self.assertEqual(RemoteHostStore.parse_jump_spec("[::1]:2200", default_user="app")["port"], 2200)
        for bad in ("", "host:abc", "::1"):
            with self.assertRaises(ValueError):
                RemoteHostStore.parse_jump_spec(bad, default_user="app")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RemoteHostStore(root / "hosts.db", root / "master.key")
            host = store.add_host(name="inner", hostname="10.0.0.9", port=22, username="app",
                                  auth_type="password", credential="target-secret",
                                  host_key="", fingerprint="",
                                  jump_spec="ops@bastion.example:2222")
            self.assertEqual(host["jump_spec"], "ops@bastion.example:2222")
            chain = store.jump_chain(host)
            self.assertEqual(len(chain), 1)
            self.assertTrue(chain[0]["inline"])
            self.assertEqual(chain[0]["username"], "ops")
            self.assertEqual(chain[0]["port"], 2222)
            # it borrows the target's own credential, which keeps secrets in one place
            self.assertEqual(chain[0]["credential"], "target-secret")

            runner = SSHRunner({**host, "jump_chain": chain})
            with tempfile.TemporaryDirectory() as nested:
                proxy, _ = runner.build_jump_proxy(Path(nested))
            self.assertIn("ops@bastion.example", proxy)
            self.assertIn("-p 2222", proxy)

            # a saved hop and a typed one are mutually exclusive
            with self.assertRaises(ValueError):
                store.add_host(name="clash", hostname="10.0.0.10", port=22, username="app",
                               auth_type="agent", credential="", host_key="", fingerprint="",
                               jump_host_id=host["id"], jump_spec="bastion.example")

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
        # discovery scans only the newest files' tails, never whole 300 MB logs
        self.assertIn("ls -t", command)
        self.assertIn("head -3", command)
        self.assertIn("grep -qF", command)
        self.assertNotIn("grep -lF", command)
        self.assertLess(command.index("tail -c"), command.index("python3"))
        self.assertNotIn("--pane", command)  # the old python reader invocation is gone
        self.assertEqual([item["text"] for item in data["messages"]], ["问题", "回答"])

    def test_incremental_transcript_only_transfers_new_bytes(self):
        runner = self.make_runner()
        calls = []

        first_lines = [
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "id": "m1",
                                                             "content": [{"type": "input_text", "text": "问题一"}]}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "id": "m2",
                                                             "content": [{"type": "output_text", "text": "回答一"}]}}),
        ]
        discovery = (b"TMUXWEB_PROVIDER<::>codex\nTMUXWEB_SESSION<::>abc\nTMUXWEB_FORMAT<::>jsonl\n"
                     b"TMUXWEB_PRIMARY<::>/home/user/.codex/sessions/a.jsonl\n"
                     b"TMUXWEB_STAMP<::>1793000000 " + str(len("\n".join(first_lines) + "\n")).encode() +
                     b"\nTMUXWEB_PAYLOAD\n") + ("\n".join(first_lines) + "\n").encode()

        appended = [json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "id": "m3", "content": [{"type": "output_text", "text": "回答二"}]}})]
        size_after = len("\n".join(first_lines) + "\n") + len(appended[0] + "\n")
        follow = (b"TMUXWEB_MODE<::>append\nTMUXWEB_STAMP<::>1793000005 " + str(size_after).encode() +
                  b"\nTMUXWEB_PAYLOAD\n") + (appended[0] + "\n").encode()

        class Result:
            def __init__(self, stdout):
                self.stdout, self.stderr, self.returncode = stdout, b"", 0

        def fake_run(command, **kwargs):
            calls.append(command)
            return Result(discovery if "TMUXWEB_MODE" not in command and "tail -c 262144" in command else follow)
            # discovery script contains the Claude/Codex locator; the follow script stats + tails

        runner.run = fake_run
        first = runner.transcript("%9", 20)
        self.assertEqual([item["text"] for item in first["messages"]], ["问题一", "回答一"])
        TRANSCRIPT_CACHE.clear()  # simulate the next poll after the 2s TTL
        self.assertIn("TMUXWEB_PAYLOAD", discovery.decode())

        second = runner.transcript("%9", 20)
        # the follow-up command carries the file and the byte offset, not a refetch
        self.assertIn("/home/user/.codex/sessions/a.jsonl", calls[-1])
        self.assertIn(str(len("\n".join(first_lines) + "\n")), calls[-1])
        self.assertEqual([item["text"] for item in second["messages"]], ["问题一", "回答一", "回答二"])

    def test_incremental_transcript_resets_when_the_file_is_rewritten(self):
        runner = self.make_runner()
        calls = []
        line = json.dumps({"role": "assistant", "content": "旧内容"}) + "\n"
        rebuilt = json.dumps({"role": "assistant", "content": "新内容"}) + "\n"
        discovery = (b"TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s1\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
                     b"TMUXWEB_PRIMARY<::>/home/user/.hermes/sessions/s1.jsonl\nTMUXWEB_STAMP<::>1793000000 "
                     + str(len(line)).encode() + b"\nTMUXWEB_PAYLOAD\n") + line.encode()
        rewritten = (b"TMUXWEB_MODE<::>full\nTMUXWEB_STAMP<::>1793000100 " + str(len(rebuilt)).encode() +
                     b"\nTMUXWEB_PAYLOAD\n") + rebuilt.encode()

        class Result:
            def __init__(self, stdout):
                self.stdout, self.stderr, self.returncode = stdout, b"", 0

        def fake_run(command, **kwargs):
            calls.append(command)
            return Result(discovery if len(calls) == 1 else rewritten)

        runner.run = fake_run
        first = runner.transcript("%9", 20)
        self.assertEqual([item["text"] for item in first["messages"]], ["旧内容"])
        TRANSCRIPT_CACHE.clear()
        second = runner.transcript("%9", 20)
        self.assertEqual([item["text"] for item in second["messages"]], ["新内容"])

    def test_incremental_transcript_falls_back_to_discovery_when_the_file_vanishes(self):
        runner = self.make_runner()
        calls = []
        line = json.dumps({"role": "assistant", "content": "仍在"}) + "\n"
        discovery = (b"TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s1\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
                     b"TMUXWEB_PRIMARY<::>/home/user/.hermes/sessions/s1.jsonl\nTMUXWEB_STAMP<::>1793000000 "
                     + str(len(line)).encode() + b"\nTMUXWEB_PAYLOAD\n") + line.encode()
        missing = b"TMUXWEB_ERROR<::>file missing\n"

        class Result:
            def __init__(self, stdout, returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, b"", returncode

        def fake_run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return Result(discovery)
            if len(calls) == 2:
                return Result(missing, returncode=25)
            return Result(discovery)

        runner.run = fake_run
        runner.transcript("%9", 20)
        TRANSCRIPT_CACHE.clear()
        runner.transcript("%9", 20)  # follow fails -> full discovery runs again
        TRANSCRIPT_CACHE.clear()
        self.assertEqual(len(calls), 3)
        self.assertEqual([item["text"] for item in runner.transcript("%9", 20)["messages"]], ["仍在"])

    def test_gzipped_payloads_are_decoded(self):
        import gzip as gziplib
        payload = "\n".join([
            json.dumps({"role": "user", "content": "压缩的问题"}),
            json.dumps({"role": "assistant", "content": "压缩的回答"}),
        ]) + "\n"
        stdout = (b"TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s9\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
                  b"TMUXWEB_ENCODING<::>gzip\nTMUXWEB_PAYLOAD\n") + gziplib.compress(payload.encode())
        header, decoded = parse_transcript_response(stdout)
        self.assertEqual(header["TMUXWEB_ENCODING"], "gzip")
        self.assertEqual(decoded, payload)

        runner = self.make_runner()

        class Result:
            def __init__(self, stdout):
                self.stdout, self.stderr, self.returncode = stdout, b"", 0

        runner.run = lambda command, **kwargs: Result(stdout)
        data = runner.transcript("%9", 10)
        self.assertEqual([item["text"] for item in data["messages"]], ["压缩的问题", "压缩的回答"])

    def test_a_broken_gzip_payload_is_reported(self):
        stdout = (b"TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s9\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
                  b"TMUXWEB_ENCODING<::>gzip\nTMUXWEB_PAYLOAD\nnot-gzip")
        with self.assertRaisesRegex(RuntimeError, "cannot decode"):
            parse_transcript_response(stdout)

    def test_follow_rediscovers_when_the_pane_starts_a_new_session(self):
        runner = self.make_runner()
        calls = []
        old_line = json.dumps({"role": "assistant", "content": "旧会话"}) + "\n"
        new_line = json.dumps({"role": "assistant", "content": "新会话"}) + "\n"
        discovery = (
            "TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s1\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
            "TMUXWEB_TITLE<::>旧任务\nTMUXWEB_PRIMARY<::>/home/user/.hermes/sessions/s1.jsonl\n"
            f"TMUXWEB_STAMP<::>1793000000 {len(old_line.encode())}\nTMUXWEB_PAYLOAD\n" + old_line
        ).encode()
        # the pinned file is still there and unchanged, but the pane title moved on
        follow = (
            "TMUXWEB_MODE<::>append\nTMUXWEB_TRUNCATED<::>0\nTMUXWEB_ENCODING<::>none\n"
            "TMUXWEB_TITLE<::>新任务\n"
            f"TMUXWEB_STAMP<::>1793000000 {len(old_line.encode())}\nTMUXWEB_PAYLOAD\n"
        ).encode()
        rediscovered = (
            "TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s2\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
            "TMUXWEB_TITLE<::>新任务\nTMUXWEB_PRIMARY<::>/home/user/.hermes/sessions/s2.jsonl\n"
            f"TMUXWEB_STAMP<::>1793000100 {len(new_line.encode())}\nTMUXWEB_PAYLOAD\n" + new_line
        ).encode()

        class Result:
            def __init__(self, stdout):
                self.stdout, self.stderr, self.returncode = stdout, b"", 0

        def fake_run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return Result(discovery)
            if "tail -c +" in command:
                return Result(follow)
            return Result(rediscovered)

        runner.run = fake_run
        first = runner.transcript("%9", 20)
        self.assertEqual([message["text"] for message in first["messages"]], ["旧会话"])
        TRANSCRIPT_CACHE.clear()
        second = runner.transcript("%9", 20)
        # the stale file is abandoned instead of being tailed forever
        self.assertEqual([message["text"] for message in second["messages"]], ["新会话"])
        self.assertEqual(len(calls), 3)

    def test_relocation_interval_forces_a_lookup(self):
        runner = self.make_runner()
        line = json.dumps({"role": "assistant", "content": "内容"}) + "\n"
        discovery = (
            "TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s1\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
            "TMUXWEB_TITLE<::>任务\nTMUXWEB_PRIMARY<::>/home/user/.hermes/sessions/s1.jsonl\n"
            f"TMUXWEB_STAMP<::>1793000000 {len(line.encode())}\nTMUXWEB_PAYLOAD\n" + line
        ).encode()
        calls = []

        class Result:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = discovery, b"", 0

        runner.run = lambda command, **kwargs: (calls.append(command), Result())[1]
        runner.transcript("%9", 20)
        TRANSCRIPT_CACHE.clear()
        TRANSCRIPT_STATE[("0123456789abcdef", "%9")]["relocated_at"] = 0
        with mock.patch.dict("os.environ", {"TMUXWEB_TRANSCRIPT_RELOCATE": "1"}):
            runner.transcript("%9", 20)
        self.assertIn("pane=$1", calls[-1])  # discovery ran again instead of the tail

    def test_refresh_drops_the_incremental_state(self):
        runner = self.make_runner()
        line = json.dumps({"role": "assistant", "content": "内容"}) + "\n"
        discovery = (
            "TMUXWEB_PROVIDER<::>hermes\nTMUXWEB_SESSION<::>s1\nTMUXWEB_FORMAT<::>hermes-jsonl\n"
            "TMUXWEB_TITLE<::>任务\nTMUXWEB_PRIMARY<::>/home/user/.hermes/sessions/s1.jsonl\n"
            f"TMUXWEB_STAMP<::>1793000000 {len(line.encode())}\nTMUXWEB_PAYLOAD\n" + line
        ).encode()

        class Result:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = discovery, b"", 0

        calls = []
        runner.run = lambda command, **kwargs: (calls.append(command), Result())[1]
        runner.transcript("%9", 20)
        self.assertIn(("0123456789abcdef", "%9"), TRANSCRIPT_STATE)
        runner.transcript("%9", 20, refresh=True)
        self.assertIn("pane=$1", calls[-1])

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
        TRANSCRIPT_STATE.clear()
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

    def test_probe_finds_tmux_outside_the_noninteractive_path(self):
        with tempfile.TemporaryDirectory() as home:
            binary = Path(home, "bin", "tmux")
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            completed = subprocess.run(
                ["/bin/sh", "-c", remote_hosts.TMUX_PROBE_SCRIPT], capture_output=True, text=True,
                env={"HOME": home, "PATH": str(binary.parent)},
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(binary))

    def test_the_tmux_path_is_saved_with_the_host(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RemoteHostStore(Path(directory) / "hosts.db", Path(directory) / "key")
            host = store.add_host(name="mac", hostname="10.0.0.8", port=22, username="mars",
                                  auth_type="agent", credential="", host_key="", fingerprint="")
            self.assertEqual(host.get("tmux_path", ""), "")
            updated = store.update_host(host["id"], tmux_path="/opt/homebrew/bin/tmux")
            self.assertEqual(updated["tmux_path"], "/opt/homebrew/bin/tmux")
            self.assertEqual(store.get_host(host["id"])["tmux_path"], "/opt/homebrew/bin/tmux")
            # editing something else must not drop it
            renamed = store.update_host(host["id"], name="mac-mini")
            self.assertEqual(renamed["tmux_path"], "/opt/homebrew/bin/tmux")

    def test_a_saved_tmux_path_skips_the_probe(self):
        host = dict(FAKE_SSH_HOST, tmux_path="/opt/homebrew/bin/tmux")
        clear_tmux_cache(host)
        runner = SSHRunner(host)

        def must_not_probe(*args, **kwargs):
            raise AssertionError("a saved tmux path must not trigger a probe")

        runner.run = must_not_probe
        self.assertEqual(runner.tmux_binary(), "/opt/homebrew/bin/tmux")

    def test_a_successful_probe_hands_the_path_to_the_app(self):
        host = dict(FAKE_SSH_HOST)
        clear_tmux_cache(host)
        runner = SSHRunner(host)

        class Result:
            returncode = 0
            stdout = b"/opt/homebrew/bin/tmux\n"
            stderr = b""

        runner.run = lambda command, **kwargs: Result()
        saved = []
        runner.on_tmux_bin = saved.append
        self.assertEqual(runner.tmux_binary(), "/opt/homebrew/bin/tmux")
        self.assertEqual(saved, ["/opt/homebrew/bin/tmux"])

    def test_the_terminal_attach_uses_the_resolved_tmux(self):
        """`zsh: command not found: tmux` came from the PTY attach missing TMUX_BIN."""
        host = dict(FAKE_SSH_HOST, id="mac", tmux_path="/opt/homebrew/bin/tmux")
        clear_tmux_cache(host)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch("os.openpty", return_value=(9, 10)), \
                mock.patch("os.close"), \
                mock.patch.object(remote_hosts.RemoteTerminalSession, "_set_size"):
            session = remote_hosts.RemoteTerminalSession(host, "%13", 80, 24)
            argv = popen.call_args[0][0]
            remote = argv[-1]
        self.assertIn("export TMUX_BIN=/opt/homebrew/bin/tmux", remote)
        self.assertIn("attach-session -t %13", remote)
        self.assertEqual(session.pane_id, "%13")

    def test_capture_many_batches_panes_into_one_call(self):
        """The status sweep must not spend one ssh round trip per pane."""
        host = dict(FAKE_SSH_HOST)
        runner = SSHRunner(host)
        seen = {}

        class Result:
            returncode = 0
            stderr = b""
            stdout = ("\x1e%1\n❯ \n\x1e%2\n\x1b[31mesc to interrupt\x1b[0m\n").encode()

        def run(command, **kwargs):
            seen["command"] = command
            return Result()

        runner.run = run
        texts = runner.capture_many(["%1", "%2", "not-a-pane"], 40)
        self.assertEqual(sorted(texts), ["%1", "%2"])
        self.assertIn("❯", texts["%1"])
        self.assertNotIn("\x1b[", texts["%2"])          # ansi stripped locally
        self.assertIn("esc to interrupt", texts["%2"])
        self.assertEqual(texts["%1"].strip(), "❯")
        self.assertIn("for pane in", seen["command"])
        self.assertIn("capture-pane", seen["command"])
        self.assertEqual(relay_status(texts["%1"]), "idle")
        self.assertEqual(relay_status(texts["%2"]), "busy")

    def test_a_slow_host_keeps_its_transcript_parse_longer(self):
        remote_hosts._TRANSCRIPT_TTL.pop("slowhost", None)
        self.assertEqual(remote_hosts.transcript_ttl("slowhost"), remote_hosts.TRANSCRIPT_CACHE_TTL)
        remote_hosts.note_transcript_latency("slowhost", 4.6)
        self.assertEqual(remote_hosts.transcript_ttl("slowhost"), 4.6)
        remote_hosts.note_transcript_latency("slowhost", 0.1)   # never below the floor
        self.assertEqual(remote_hosts.transcript_ttl("slowhost"), remote_hosts.TRANSCRIPT_CACHE_TTL)
        remote_hosts.note_transcript_latency("slowhost", 60)    # and never unbounded
        self.assertEqual(remote_hosts.transcript_ttl("slowhost"), 10.0)
        remote_hosts._TRANSCRIPT_TTL.pop("slowhost", None)

    def test_the_transcript_window_adapts_to_the_link(self):
        """No knob: a slow pull halves the tail, a fast one grows it back."""
        host = dict(FAKE_SSH_HOST, id="adaptive")
        remote_hosts._PAYLOAD_WINDOW.pop("adaptive", None)
        self.assertEqual(remote_hosts.payload_window(host), remote_hosts.PAYLOAD_WINDOW_DEFAULT)
        remote_hosts.note_pull_duration(host, 12.0)
        self.assertEqual(remote_hosts.payload_window(host), remote_hosts.PAYLOAD_WINDOW_DEFAULT // 2)
        for _ in range(10):
            remote_hosts.note_pull_duration(host, 30.0)
        self.assertEqual(remote_hosts.payload_window(host), remote_hosts.PAYLOAD_WINDOW_MIN)
        self.assertGreaterEqual(remote_hosts.PAYLOAD_WINDOW_MIN, 1024 * 1024)  # keep enough tail for history
        for _ in range(8):
            remote_hosts.note_pull_duration(host, 0.4)
        self.assertEqual(remote_hosts.payload_window(host), remote_hosts.PAYLOAD_WINDOW_MAX)
        # a middling pull keeps whatever was learned
        before = remote_hosts.payload_window(host)
        remote_hosts.note_pull_duration(host, 5.0)
        self.assertEqual(remote_hosts.payload_window(host), before)
        runner = SSHRunner(host)
        argv, _environment = runner.command("true")
        self.assertIn(f"export TMUXWEB_PAYLOAD_BYTES={before};", argv[-1])
        remote_hosts._PAYLOAD_WINDOW.pop("adaptive", None)

    def test_a_stalled_transcript_blames_the_link(self):
        """A hung ssh must surface as 'slow network', not as a broken transcript."""
        host = dict(FAKE_SSH_HOST)
        clear_tmux_cache(host)
        runner = SSHRunner(host)

        def stall(*args, **kwargs):
            raise RuntimeError("remote command timed out after 30s")

        runner.run = stall
        with self.assertRaises(RuntimeError) as caught:
            runner.transcript("%10", 20)
        self.assertIn("网络", str(caught.exception))
        self.assertLessEqual(runner.transcript_timeout, 600)

    def test_the_transcript_timeout_is_configurable(self):
        host = dict(FAKE_SSH_HOST)
        runner = SSHRunner(host)
        with patch.dict(os.environ, {"TMUX_WEB_TRANSCRIPT_TIMEOUT": "12"}):
            self.assertEqual(runner.transcript_timeout, 12)
        with patch.dict(os.environ, {"TMUX_WEB_TRANSCRIPT_TIMEOUT": "0"}):
            self.assertGreaterEqual(runner.transcript_timeout, 5)   # never spin instantly

    def test_probe_is_empty_when_tmux_is_missing(self):
        with tempfile.TemporaryDirectory() as home:
            completed = subprocess.run(
                ["/bin/sh", "-c", remote_hosts.TMUX_PROBE_SCRIPT], capture_output=True, text=True,
                env={"HOME": home, "PATH": str(Path(home, "empty"))},
            )
        self.assertEqual(completed.stdout.strip(), "")

    def test_a_host_without_tmux_gets_an_actionable_error(self):
        host = dict(FAKE_SSH_HOST)
        clear_tmux_cache(host)
        runner = SSHRunner(host)

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""

        runner.run = lambda command, **kwargs: Result()
        with self.assertRaises(RuntimeError) as caught:
            runner.tmux_binary()
        message = str(caught.exception)
        self.assertIn("brew install tmux", message)
        self.assertIn("/opt/homebrew/bin", message)

    def test_probe_knows_common_mac_prefixes(self):
        for path in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/opt/local/bin/tmux"):
            self.assertIn(path, remote_hosts.TMUX_PROBE_SCRIPT)

    def test_tmux_path_can_be_forced_through_the_environment(self):
        host = dict(FAKE_SSH_HOST)
        runner = SSHRunner(host)
        with mock.patch.dict("os.environ", {"TMUX_WEB_TMUX_PATH": "/custom/bin/tmux"}):
            self.assertEqual(runner.tmux_binary(), "/custom/bin/tmux")

    def test_an_unpinned_host_is_accepted_once_and_then_remembered(self):
        host = dict(FAKE_SSH_HOST, host_key="")
        runner = SSHRunner(host)
        self.assertFalse(runner.host_key_pinned())
        command, _ = runner.command("true", host_key_pinned=False)
        self.assertIn("StrictHostKeyChecking=accept-new", " ".join(command))
        pinned_command, _ = runner.command("true", host_key_pinned=True)
        self.assertIn("StrictHostKeyChecking=yes", " ".join(pinned_command))

        captured = []
        runner.on_host_key = captured.append
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "known_hosts")
            path.write_text("10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbc\n")
            runner._remember_host_key(path)
        self.assertEqual(captured, ["10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbc"])

    def test_remote_commands_export_the_resolved_binary(self):
        host = dict(FAKE_SSH_HOST)
        runner = SSHRunner(host)
        command, _ = runner.command("true", tmux_bin="/opt/homebrew/bin/tmux")
        self.assertIn("export TMUX_BIN=/opt/homebrew/bin/tmux;", command[-1])
        plain, _ = runner.command("true")
        self.assertNotIn("TMUX_BIN=", plain[-1])

        runner.tmux_binary = lambda: "/opt/homebrew/bin/tmux"
        seen = {}
        original = runner.command

        def capture(remote_command, **kwargs):
            seen.update(kwargs)
            return original(remote_command, **kwargs)

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"ok", stderr=b"")
        runner.command = capture
        with mock.patch("remote_hosts.subprocess.run", return_value=completed):
            runner.run("printf done", timeout=1)
        self.assertEqual(seen.get("tmux_bin"), "/opt/homebrew/bin/tmux")

    def test_remote_download_reports_a_missing_file(self):
        runner = self.make_runner()

        class Result:
            def __init__(self):
                self.stdout = b""
                self.stderr = "no such file: /srv/work/gone.md".encode()
                self.returncode = 44

        runner.run = lambda command, **kwargs: Result()
        with self.assertRaisesRegex(RuntimeError, "no such file"):
            runner.download_file("/srv/work/gone.md", 1024)

    def test_remote_download_probes_then_quotes_odd_paths(self):
        runner = self.make_runner()
        seen = []

        class Result:
            def __init__(self):
                self.stdout, self.stderr, self.returncode = b"data", b"", 0

        runner.run = lambda command, **kwargs: (seen.append(command), Result())[1]
        self.assertEqual(runner.download_file("/srv/work/ITER5H_结论.md", 16), b"data")
        self.assertIn("[ ! -f ", seen[-1])
        self.assertIn("ITER5H_结论.md", seen[-1])
        self.assertIn("head -c 17", seen[-1])

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
