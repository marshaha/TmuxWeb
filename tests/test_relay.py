import json
import unittest

import relay


class FakeTransport:
    """Minimal transport: capture() and send() are all the relay needs."""

    def __init__(self, screen="❯ ", replies=None):
        self.screen = screen
        self.replies = list(replies or [])
        self.sent = []
        self.captures = 0

    def capture(self, pane_id, history=500):
        self.captures += 1
        if self.replies:
            self.screen = self.replies.pop(0)
        return self.screen

    def send(self, pane_id, text, submit=True):
        self.sent.append((pane_id, text, submit))


class ClassifyTests(unittest.TestCase):
    def test_codex_idle_prompt(self):
        self.assertEqual(relay.classify("output\n\u203a Ask Codex to do anything\n"), "idle")

    def test_hermes_idle_prompt_below_status_bar(self):
        screen = " ⚕ deepseek-flash │ 55.4K/128K │ 43% │ 6h 34m\n❯\n──────────────\n"
        self.assertEqual(relay.classify(screen), "idle")

    def test_shell_prompt_is_idle(self):
        self.assertEqual(relay.classify("$ "), "idle")
        self.assertEqual(relay.classify("congyong@host:~/repo$ "), "idle")

    def test_spinner_and_escape_hints_mean_busy(self):
        self.assertEqual(relay.classify("回答中\n⠋ Thinking…"), "busy")
        self.assertEqual(relay.classify("running\n(esc to interrupt)"), "busy")

    def test_a_kimi_status_bar_is_not_a_running_turn(self):
        """`K3.1 Pro thinking: high` is a model setting, not progress."""
        screen = ("  ● 已合入并部署\n"
                  "  ╭──────────────────────────────\n"
                  "  │ > 拉取新的pr.\n"
                  "  ╰──────────────────────────────\n"
                  "  Never Ask  K3.1 Pro thinking: high  ~/workspace/auto_verilog\n")
        self.assertEqual(relay.classify(screen), "idle")

    def test_plain_output_is_unknown_not_busy(self):
        self.assertEqual(relay.classify("compiling foo.c\nlinking foo\n"), "unknown")

    def test_soft_progress_needs_an_ellipsis(self):
        self.assertEqual(relay.classify("✻ Thinking… (12s)"), "busy")
        self.assertEqual(relay.classify("thinking: high"), "unknown")

    def test_empty_screen_is_unknown(self):
        self.assertEqual(relay.classify(""), "unknown")

    def test_ansi_codes_do_not_hide_the_prompt(self):
        self.assertEqual(relay.classify("\x1b[32m❯\x1b[0m\n"), "idle")


class ReplyExtractionTests(unittest.TestCase):
    TOKEN = "0123456789abcdef01234567"

    def test_prompt_line_is_not_mistaken_for_a_reply(self):
        prompt = relay.build_prompt("问题", self.TOKEN, "relay_cli")
        ignore = {line.strip() for line in prompt.splitlines() if line.strip()}
        self.assertIsNone(relay.extract_reply(prompt, self.TOKEN, ignore))

    def test_marked_json_reply_is_returned(self):
        text = "thinking...\n" + relay.marker(self.TOKEN) + ' "answer text"\n'
        self.assertEqual(relay.extract_reply(text, self.TOKEN), "answer text")

    def test_reply_with_escaped_newline_is_decoded(self):
        text = relay.marker(self.TOKEN) + " " + json.dumps("line one\nline two") + "\n"
        self.assertEqual(relay.extract_reply(text, self.TOKEN), "line one\nline two")

    def test_newest_valid_reply_wins(self):
        text = "\n".join([
            relay.marker(self.TOKEN) + ' "first"',
            relay.marker(self.TOKEN) + ' "second"',
        ])
        self.assertEqual(relay.extract_reply(text, self.TOKEN), "second")

    def test_unquoted_remainder_is_used_when_json_is_broken(self):
        text = relay.marker(self.TOKEN) + ' "unterminated reply\n'
        self.assertEqual(relay.extract_reply(text, self.TOKEN), "unterminated reply")

    def test_markdown_fence_around_marker_is_tolerated(self):
        text = relay.marker(self.TOKEN) + ' `"fenced"`'
        self.assertEqual(relay.extract_reply(text, self.TOKEN), "fenced")


class AskTests(unittest.TestCase):
    def test_ask_sends_marked_prompt_and_returns_reply(self):
        token_holder = {}

        def reply_later(transport):
            marker = None
            for _, text, _submit in transport.sent:
                for word in text.split():
                    if word.startswith("TMUXWEB_REPLY_"):
                        marker = word
            token_holder["marker"] = marker
            transport.screen = "❯ " + (marker or "") + ' "ok"'

        transport = FakeTransport()

        original_send = transport.send

        def send(pane_id, text, submit=True):
            original_send(pane_id, text, submit)
            reply_later(transport)

        transport.send = send
        result = relay.ask(transport, "%7", "你好", source="test", timeout=10, poll_interval=0.01)
        self.assertEqual(result["reply"], "ok")
        self.assertEqual(result["pane"], "%7")
        self.assertIn("TMUXWEB_RELAY", transport.sent[0][1])
        self.assertIn("你好", transport.sent[0][1])
        self.assertEqual(transport.sent[0][2], True)

    def test_busy_pane_is_rejected_unless_forced(self):
        busy = FakeTransport(screen="⠋ Thinking…\n(esc to interrupt)")
        with self.assertRaises(relay.RelayBusy):
            relay.ask(busy, "%7", "你好", timeout=5)
        self.assertEqual(busy.sent, [])

    def test_timeout_reports_pane_tail(self):
        transport = FakeTransport(screen="❯ ", replies=["❯ \n" for _ in range(50)])
        with self.assertRaises(relay.RelayTimeout) as caught:
            relay.ask(transport, "%7", "你好", timeout=5, poll_interval=0.01)
        self.assertIn("timed out", str(caught.exception))
        self.assertIn("❯", caught.exception.tail)

    def test_empty_question_is_rejected(self):
        with self.assertRaises(relay.RelayError):
            relay.ask(FakeTransport(), "%7", "   ", timeout=5)


if __name__ == "__main__":
    unittest.main()
