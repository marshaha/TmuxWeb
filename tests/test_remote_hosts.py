import tempfile
import unittest
from pathlib import Path

from remote_hosts import RemoteHostStore, SSHRunner, validate_private_key_text, parse_tmux_panes, pane_ref, split_pane_ref


class RemoteHostTests(unittest.TestCase):
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
        raw = "%4\ts:0.1\ts\t0\tmain\t1\tclaude\tReview\t/work\t1\t120\t40\n"
        host_id = "0123456789abcdef"
        panes = parse_tmux_panes(raw, host_id)
        self.assertEqual(panes[0]["id"], f"{host_id}~%4")
        self.assertEqual(panes[0]["pane_id"], "%4")
        self.assertEqual(panes[0]["window_name"], "main")
        self.assertEqual(split_pane_ref(panes[0]["id"]), (host_id, "%4"))
        self.assertEqual(pane_ref("localhost", "%4"), "%4")

    def test_remote_tmux_format_uses_real_tabs(self):
        runner = SSHRunner({"id": "0123456789abcdef", "hostname": "host", "port": 22,
                            "username": "user", "auth_type": "password", "credential": "x",
                            "host_key": "host ssh-ed25519 AAAA"})
        captured = {}
        class Result:
            returncode = 0
            stdout = b""
            stderr = b""
        runner.run = lambda command: captured.setdefault("command", command) or Result()
        # Avoid the lambda truthy return shortcut by recording through a helper.
        def fake_run(command):
            captured["command"] = command
            return Result()
        runner.run = fake_run
        runner.list_panes()
        self.assertIn("\t", captured["command"])
        self.assertNotIn("\\t", captured["command"])

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
