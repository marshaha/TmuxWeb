"""agent_catalog: which agent CLIs a host can start, and how to build the argv."""

import shlex
import unittest
from unittest import mock

import agent_catalog


class ParseProbeTests(unittest.TestCase):
    def test_reads_name_and_path_and_keeps_catalog_order(self):
        text = "codex\t/usr/local/bin/codex\nhermes\t/home/u/.local/bin/hermes\n"
        found = agent_catalog.parse_probe(text)
        self.assertEqual([item["name"] for item in found], ["hermes", "codex"])
        self.assertEqual(found[0]["path"], "/home/u/.local/bin/hermes")
        self.assertEqual(found[0]["label"], "Hermes")

    def test_ignores_noise_and_unknown_names(self):
        text = "bash: warning: something\nnope\t/x\ncodex\n\nclaude\t\n"
        self.assertEqual(agent_catalog.parse_probe(text), [])

    def test_probe_script_expands_home_instead_of_literal_tilde(self):
        script = agent_catalog.PROBE_SCRIPT
        self.assertIn('"$HOME/.local/bin"', script)
        self.assertNotIn('"~/', script, "引号里的 ~ 不会展开，探测会漏掉 ~/.local/bin 里的 agent")

    def test_probe_script_is_posix_and_needs_no_python(self):
        script = agent_catalog.PROBE_SCRIPT
        self.assertIn('printf "%s\\t%s\\n"', script)
        self.assertNotIn("bash ", script)
        self.assertNotIn("python", script)
        # macOS 的 /bin/sh 没有 timeout；探测不能依赖它
        self.assertNotIn("timeout ", script)
        for spec in agent_catalog.AGENTS:
            self.assertIn(spec["name"], script)


class CommandTests(unittest.TestCase):
    def test_args_are_split_like_a_shell(self):
        self.assertEqual(agent_catalog.command_for("codex", "--model o3 -c 'a b'"),
                         ["codex", "--model", "o3", "-c", "a b"])

    def test_empty_args_are_allowed(self):
        self.assertEqual(agent_catalog.command_for("claude", ""), ["claude"])

    def test_case_insensitive_names(self):
        self.assertEqual(agent_catalog.command_for("Claude"), ["claude"])

    def test_unknown_agent_is_an_error(self):
        with self.assertRaises(ValueError):
            agent_catalog.command_for("definitely-not-installed")

    def test_broken_quoting_is_an_error(self):
        with self.assertRaises(ValueError):
            agent_catalog.command_for("codex", '--x "unclosed')

    def test_render_quotes_for_tmux(self):
        self.assertEqual(agent_catalog.render(["codex", "-c", "a b"]), "codex -c 'a b'")
        argv = ["codex", "--x=$HOME", "two words", "it's"]
        self.assertEqual(shlex.split(agent_catalog.render(argv)), argv)


class LocalProbeTests(unittest.TestCase):
    def test_found_binaries_come_back_with_a_path(self):
        def which(binary):
            return "/opt/bin/%s" % binary if binary == "codex" else None

        with mock.patch("agent_catalog.shutil.which", side_effect=which):
            with mock.patch("agent_catalog.EXTRA_DIRS", ()):
                found = agent_catalog.probe_local()
        self.assertEqual([item["name"] for item in found], ["codex"])
        self.assertEqual(found[0]["path"], "/opt/bin/codex")

    def test_extra_directories_are_checked_when_path_is_thin(self):
        with mock.patch("agent_catalog.shutil.which", return_value=None):
            with mock.patch("agent_catalog.EXTRA_DIRS", ("/definitely/not/here",)):
                self.assertEqual(agent_catalog.probe_local(), [])


if __name__ == "__main__":
    unittest.main()
