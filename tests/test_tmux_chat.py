import json
import tempfile
import unittest
from pathlib import Path

from tmux_chat import Registry, extract_reply, pane_is_ready


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "panes.json"
        self.registry = Registry(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_resolve_fixed_pane(self):
        self.registry.register("claude", "%0", "claude")
        entry = self.registry.resolve("claude")
        self.assertEqual(entry, {"pane": "%0", "kind": "claude"})

    def test_rejects_invalid_alias_and_pane(self):
        with self.assertRaises(ValueError):
            self.registry.register("bad alias", "%0", "claude")
        with self.assertRaises(ValueError):
            self.registry.register("claude", "$(touch /tmp/x)", "claude")


class PaneParsingTests(unittest.TestCase):
    def test_detects_ready_prompts(self):
        self.assertTrue(pane_is_ready("claude", "status\n❯ \nfooter"))
        self.assertTrue(pane_is_ready("codex", "\n› Ask Codex to do anything\n"))
        self.assertTrue(pane_is_ready("hermes", "\n❯ Draft a reply\n"))
        self.assertFalse(pane_is_ready("claude", "● Running tests…"))

    def test_extracts_last_valid_json_reply(self):
        token = "abc123"
        prefix = f"TMUX_BRIDGE_REPLY_{token}:"
        text = (
            f"prompt mentions {prefix} <JSON string>\n"
            f"some prose\n{prefix} \"第一行\\n第二行\"\n"
        )
        self.assertEqual(extract_reply(text, token), "第一行\n第二行")

    def test_extracts_json_object_reply_used_by_hermes(self):
        text = 'TMUX_BRIDGE_REPLY_h1: {"status":"ok","message":"HERMES_TMUX_OK"}'
        self.assertEqual(extract_reply(text, "h1"), "HERMES_TMUX_OK")
        text = 'TMUX_BRIDGE_REPLY_h2: {"response":"HERMES_TMUX_FINAL_OK"}'
        self.assertEqual(extract_reply(text, "h2"), "HERMES_TMUX_FINAL_OK")

    def test_returns_none_before_valid_reply(self):
        self.assertIsNone(extract_reply("TMUX_BRIDGE_REPLY_x: <JSON string>", "x"))


if __name__ == "__main__":
    unittest.main()
