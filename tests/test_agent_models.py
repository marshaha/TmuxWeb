"""agent_models: which agents expose a model picker, and what to type."""

import unittest

import agent_models


class SwitchCommandTests(unittest.TestCase):
    def test_argument_form_for_agents_that_take_one(self):
        self.assertEqual(agent_models.switch_command("claude", "opus"), "/model opus")
        self.assertEqual(agent_models.switch_command("codex", "gpt-5.6-sol"), "/model gpt-5.6-sol")
        self.assertEqual(agent_models.switch_command("hermes", "deepseek-flash"), "/model deepseek-flash")

    def test_kimi_opens_its_own_picker(self):
        # `kimi` 回 "Unknown model alias" 给任何参数，只能用无参形式
        self.assertTrue(agent_models.picker_only("kimi"))
        self.assertEqual(agent_models.switch_command("kimi", ""), "/model")
        self.assertEqual(agent_models.switch_command("kimi", "whatever"), "/model")

    def test_missing_or_dirty_model_is_rejected(self):
        with self.assertRaises(ValueError):
            agent_models.switch_command("claude", "   ")
        with self.assertRaises(ValueError):
            agent_models.switch_command("claude", "opus\nrm -rf /")
        with self.assertRaises(ValueError):
            agent_models.switch_command("claude", "x" * 81)

    def test_non_agents_have_no_picker(self):
        with self.assertRaises(ValueError):
            agent_models.switch_command("", "opus")
        with self.assertRaises(ValueError):
            agent_models.switch_command("bash", "opus")
        self.assertFalse(agent_models.supported("bash"))

    def test_presets_only_for_known_providers(self):
        self.assertIn("opus", agent_models.preset_for("claude"))
        self.assertEqual(agent_models.preset_for("bash"), [])


class ProviderLabelTests(unittest.TestCase):
    def test_vendor_labels_and_configured_route(self):
        self.assertEqual(agent_models.provider_label("claude"), "Anthropic")
        self.assertEqual(agent_models.provider_label("codex"), "OpenAI")
        self.assertEqual(agent_models.provider_label("kimi"), "Kimi Code")
        # Hermes 走哪个网关是用户自己的配置，配置了就用它
        self.assertEqual(agent_models.provider_label("hermes", "axera"), "axera")
        self.assertEqual(agent_models.provider_label("hermes"), "Hermes")
        self.assertEqual(agent_models.provider_label(""), "")

    def test_hermes_route_ignores_comments_and_lookalike_keys(self):
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as home:
            config = P(home) / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "# Hermes Agent CLI Configuration\n"
                "providers:\n"
                "  elsewhere:\n"
                "    provider: wrong\n"
                "model:\n"
                "  default: gpt-5.6-sol\n"
                "  # route for this box\n"
                "  provider: \"ctyun\"\n"
                "agent:\n"
                "  max_turns: 500\n"
            )
            self.assertEqual(agent_models.hermes_route(home), "ctyun")
            self.assertEqual(agent_models.hermes_route(home + "-missing"), "")


if __name__ == "__main__":
    unittest.main()
