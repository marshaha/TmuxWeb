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


if __name__ == "__main__":
    unittest.main()
