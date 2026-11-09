"""交互菜单的解析用真实抓屏样本做断言（kimi 的 Trust 对话框和模型选择器都是实测抓的）。"""
import unittest

import agent_prompts as prompts

# kimi 在 /tmp 打开时弹的信任确认（实测 `tmux capture-pane -p`）
TRUST = """
─────────────────────────────────────────────────────────────────────
  Trust this folder?
  ↑↓ navigate · Enter select · Esc exit

  /tmp

  No project integrations or instructions to activate.

  Trust is remembered for this folder, including future project config changes.
  Tool approvals follow your permission settings.

   ❯ Trust and continue
     Exit
 ─────────────────────────────────────────────────────────────────────
"""

# kimi `/model` 的选择器（实测，ANSI 已剥掉；屏幕外还有 24 项）
MODEL_PICKER = """
  Select a model  (type to search)
  Tab toggle provider · ↑↓ navigate · Enter select · Alt+S session-only · Esc cancel

   All   axera   ctyun   Kimi Code

     Claude Sonnet 5 5 High            ctyun
     K2.8 Preview                      Kimi Code
     K2.7 Code Highspeed               Kimi Code
     K3                                Kimi Code
     K3-256k                           Kimi Code
  ❯ K3.1 Pro                          Kimi Code ↑ current
     K3.1 Pro Highspeed 2x Usage Cost  Kimi Code
     GPT 6 Chat Thinking               ctyun
  ▼ 24 more
  Thinking  (←→ to switch)
   [ On ]    Off
 ─────────────────────────────────────────────────────────────────────
 Ask When Needed  K2.8 Preview thinking: max  ~/workspace/taijiapi    ctrl+o expand
"""

# claude 的权限确认：光标 + 序号同时出现
CLAUDE_PROCEED = """
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again this session
   3. No, and tell Claude what to do differently
 Esc to cancel
"""

COMPACT = """
 Context window is getting full.

 What would you like to do?
 1. Compact and continue
 2. Start a new session
 3. Cancel
 Enter select · Esc cancel
"""

YES_NO = """
 Apply this patch to src/app.py? [y/N]
"""

PRESS_ENTER = """
 All checks passed.
 Press Enter to continue
"""

# hermes / 普通 shell：底部那个 ❯ 是输入提示符，不是菜单
PLAIN_PROMPT = """
 ❯ 帮我看一下 tmux_web.py 的下载逻辑
   congyong@robo:~/workspace/TmuxWeb$ ls
   README.md  tmux_web.py  transfer_tickets.py
 ❯
"""


class CursorListTests(unittest.TestCase):
    def test_the_trust_dialog_lists_both_choices(self):
        found = prompts.parse_screen(TRUST)
        self.assertIsNotNone(found)
        self.assertEqual(found["kind"], "menu")
        self.assertEqual(found["title"], "Trust this folder?")
        self.assertIn("navigate", found["hint"])
        labels = [option["label"] for option in found["options"]]
        self.assertEqual(labels, ["Trust and continue", "Exit"])
        self.assertEqual(found["selected"], 0)
        self.assertFalse(found["numbers"], "没有序号的列表不能发数字")

    def test_the_model_picker_keeps_labels_and_providers_apart(self):
        found = prompts.parse_screen(MODEL_PICKER)
        self.assertIsNotNone(found)
        labels = [option["label"] for option in found["options"]]
        self.assertIn("K3.1 Pro", labels)
        self.assertIn("Claude Sonnet 5 5 High", labels)
        self.assertNotIn("All", labels, "tab 栏不是选项")
        self.assertEqual(found["more"], 24)
        selected = found["options"][found["selected"]]
        self.assertEqual(selected["label"], "K3.1 Pro")
        self.assertIn("Kimi Code", selected["meta"])
        self.assertNotIn("Off", labels, "开关不是选项")
        self.assertFalse(found["numbers"])

    def test_a_plain_shell_prompt_is_not_a_menu(self):
        self.assertIsNone(prompts.parse_screen(PLAIN_PROMPT))

    def test_a_single_line_menu_is_ignored(self):
        self.assertIsNone(prompts.parse_screen("  ❯ only one option here\n"))


class NumberedTests(unittest.TestCase):
    def test_a_numbered_list_is_marked_as_numberable(self):
        found = prompts.parse_screen(COMPACT)
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "What would you like to do?")
        self.assertEqual([option["number"] for option in found["options"]], ["1", "2", "3"])
        self.assertEqual([option["label"] for option in found["options"]],
                         ["Compact and continue", "Start a new session", "Cancel"])
        self.assertTrue(found["numbers"])

    def test_cursor_plus_numbers_keeps_the_numbers(self):
        found = prompts.parse_screen(CLAUDE_PROCEED)
        self.assertIsNotNone(found)
        self.assertTrue(found["numbers"])
        self.assertEqual(len(found["options"]), 3)
        self.assertEqual(found["selected"], 0)
        self.assertEqual(found["title"], "Do you want to proceed?")


class ConfirmTests(unittest.TestCase):
    def test_yes_no_offers_both_keys(self):
        found = prompts.parse_screen(YES_NO)
        self.assertIsNotNone(found)
        self.assertEqual(found["kind"], "confirm")
        keys = [option["key"] for option in found["options"]]
        self.assertEqual(keys, ["yes", "no"])

    def test_press_enter_offers_continue(self):
        found = prompts.parse_screen(PRESS_ENTER)
        self.assertIsNotNone(found)
        self.assertEqual([option["key"] for option in found["options"]], ["enter"])


class KeySequenceTests(unittest.TestCase):
    def test_a_numbered_choice_sends_the_digit(self):
        found = prompts.parse_screen(COMPACT)
        self.assertEqual(prompts.choice_keys(found, 1), ["2", "Enter"])

    def test_a_cursor_choice_walks_from_the_top(self):
        found = prompts.parse_screen(TRUST)
        keys = prompts.choice_keys(found, 1)
        self.assertEqual(keys.count("Up"), len(found["options"]) + 3, "先顶到第一项")
        self.assertEqual(keys[keys.index("Down"):], ["Down", "Enter"], "再往下走一格")
        self.assertEqual(keys[-1], "Enter")

    def test_the_first_option_needs_no_down(self):
        found = prompts.parse_screen(TRUST)
        keys = prompts.choice_keys(found, 0)
        self.assertNotIn("Down", keys)
        self.assertEqual(keys[-1], "Enter")

    def test_an_out_of_range_index_is_clamped(self):
        found = prompts.parse_screen(TRUST)
        self.assertEqual(prompts.choice_keys(found, 99),
                         prompts.choice_keys(found, len(found["options"]) - 1))

    def test_yes_no_and_escape_map_to_keys(self):
        found = prompts.parse_screen(YES_NO)
        self.assertEqual(prompts.choice_keys(found, 0), ["y", "Enter"])
        self.assertEqual(prompts.choice_keys(found, 1), ["n", "Enter"])
        self.assertEqual(prompts.choice_for_key(found, "esc"), ["Escape"])
        self.assertEqual(prompts.choice_for_key(found, "enter"), ["Enter"])

    def test_for_key_accepts_a_digit_for_numbered_lists(self):
        found = prompts.parse_screen(COMPACT)
        self.assertEqual(prompts.choice_for_key(found, "3"), ["3", "Enter"])

    def test_unknown_keys_are_rejected(self):
        found = prompts.parse_screen(TRUST)
        self.assertIsNone(prompts.choice_for_key(found, "launch-missiles"))


if __name__ == "__main__":
    unittest.main()
