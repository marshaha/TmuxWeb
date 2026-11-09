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

# kimi 的模型选择器 + 底部 Thinking 开关（实测）
PICKER_WITH_THINKING = """
  Select a model  (type to search)
  Tab toggle provider · ↑↓ navigate · Enter select · Alt+S session-only · Esc cancel

   All   axera   ctyun   Kimi Code

     Claude Sonnet 5 5 High            ctyun
  ❯ K3.1 Pro                          Kimi Code ← current
     K2.8 Preview                      Kimi Code
  ▼ 24 more
  Thinking  (←→ to switch)
     Low    [ High ]    Max
  Deeper reasoning for complex tasks, with higher token usage
"""

ONLY_TOGGLE = """
  Options
  Verbosity  (←→ to switch)
   [ On ]    Off
"""

# codex 的目录信任对话框（实测；注意光标是 › 而不是 ❯，问题被折成两行）
CODEX_TRUST = """
╭───────────────────────────────────────╮
│ >_ OpenAI Codex (v0.155.0)            │
╰───────────────────────────────────────╯
> You are in /tmp/twx-codex
  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt
  hooks, and exec policies to load.
› 1. Yes, continue
  2. No, quit
  Press enter to continue
"""

# codex 的更新提示（实测；标题在页脚上方，页脚 "? for shortcuts" 不能当标题）
CODEX_UPDATE = """
✨ Update available! 0.155.0 -> 0.167.0
  Release notes: https://github.com/openai/codex/releases/latest
› 1. Update now (runs `sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh'`)
  2. Skip
  3. Skip until next version
  Press enter to continue
"""

# claude code 的目录信任对话框（实测：光标列表 + 普通续行 + Enter/Esc 提示）
CLAUDE_TRUST = """
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source pro

 review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
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

# 聊天记录里用户自己打的一串编号 —— 真踩过：被当成菜单，点一下往 pane 里敲了个 "1"
NUMBERED_CHAT_TEXT = """
 回一句是哪个（或直接说要什么）就行。
────────────────────────────────────────
─ 12.3s · 2:45 PM
────────────────────────────────────────
❯ 1. 是你之前的弹窗选择 我点击了 就发送了1，但是弹窗居然一直还在。
2.  请你把粘贴板的图片直接转附件上传
────────────────────────────────────────
☤ deepseek-flash · 408K/1M · [████░░░░░░] 41%
"""

# 助手自己在聊天里贴了一段带编号的说明 + 引用了"←→ to switch"这种提示词
# 真踩过：整段被当成菜单，卡片里列出的全是聊天正文
CHAT_PROSE_QUOTING_HINTS = """
  不过你这次测试确实暴露了两个真 bug，已经修了：
  1. 开关重复出现三次：抓屏带 scrollback，解析器当成三个开关。现在按选项去重。
  2. 开关的名字被吃掉：提示可能是单独一行（←→ to switch），我之前整行跳过了。
  3. 顺手改了交互：点完之后先显示「已发送：xxx」再换下一个问题。

  第 3 步  分段开关     形态  [ Off ]  Normal  Deep
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


class ToggleTests(unittest.TestCase):
    def test_the_thinking_switch_is_parsed_next_to_the_picker(self):
        found = prompts.parse_screen(PICKER_WITH_THINKING)
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "Select a model  (type to search)")
        self.assertEqual(len(found["toggles"]), 1)
        toggle = found["toggles"][0]
        self.assertEqual(toggle["label"], "Thinking")
        self.assertEqual(toggle["options"], ["Low", "High", "Max"])
        self.assertEqual(toggle["active"], 1, "带方括号的是当前值")
        self.assertIn("←→", toggle["hint"])
        # 开关行不能被当成列表项
        self.assertNotIn("Low", [option["label"] for option in found["options"]])

    def test_a_two_state_switch_reads_both_segments(self):
        found = prompts.parse_screen(ONLY_TOGGLE)
        self.assertIsNotNone(found)
        self.assertEqual(found["kind"], "toggle")
        self.assertEqual(found["toggles"][0]["options"], ["On", "Off"])
        self.assertEqual(found["toggles"][0]["active"], 0)
        self.assertEqual(found["toggles"][0]["label"], "Verbosity")

    def test_a_switch_redrawn_several_times_is_one_switch_with_the_latest_value(self):
        """TUI 每次按键都重绘，历史里留好几份；卡片里不能出现多个同名开关。"""
        screen = ""
        for index, active in enumerate(("Off", "Normal", "Deep")):
            segments = "    ".join(
                f"[ {name} ]" if name == active else f"  {name}  "
                for name in ("Off", "Normal", "Deep")
            )
            screen += (f"  第 3 步 / 3：分段开关\n"
                       f"  ←→ to switch · Enter confirm · Esc cancel\n"
                       f"  形态\n    {segments}\n")
        found = prompts.parse_screen(screen)
        self.assertIsNotNone(found)
        self.assertEqual(len(found["toggles"]), 1)
        self.assertEqual(found["toggles"][0]["active"], 2, "要取最新那次重绘的值")

    def test_switching_uses_arrow_keys_relative_to_the_current_value(self):
        found = prompts.parse_screen(PICKER_WITH_THINKING)
        self.assertEqual(prompts.toggle_keys(found, 0, 2), ["Right"])       # High → Max
        self.assertEqual(prompts.toggle_keys(found, 0, 0), ["Left"])        # High → Low
        self.assertEqual(prompts.toggle_keys(found, 0, 1), [])              # 已经在 High

    def test_a_two_state_switch_flips_either_way(self):
        found = prompts.parse_screen(ONLY_TOGGLE)
        self.assertEqual(prompts.toggle_keys(found, 0, 1), ["Right"])
        self.assertEqual(prompts.toggle_keys(found, 0, 0), [])


class OtherAgentsTests(unittest.TestCase):
    def test_codex_trust_dialog_uses_the_question_not_the_wrapped_tail(self):
        found = prompts.parse_screen(CODEX_TRUST)
        self.assertIsNotNone(found)
        self.assertTrue(found["title"].startswith("Do you trust the contents of this directory?"))
        self.assertEqual([option["label"] for option in found["options"]],
                         ["Yes, continue", "No, quit"])
        self.assertTrue(found["numbers"])
        self.assertTrue(found["cursor"], "› 也是光标")
        # 有光标就走方向键：数字在 codex 的对话框里不生效
        self.assertNotIn("1", prompts.choice_keys(found, 0))

    def test_codex_update_dialog_ignores_the_shortcut_footer_as_title(self):
        found = prompts.parse_screen(CODEX_UPDATE)
        self.assertIsNotNone(found)
        self.assertIn("Update available", found["title"])
        self.assertNotIn("for shortcuts", found["title"])
        self.assertEqual(len(found["options"]), 3)

    def test_claude_trust_dialog_lists_plain_rows_after_the_cursor(self):
        found = prompts.parse_screen(CLAUDE_TRUST)
        self.assertIsNotNone(found)
        self.assertTrue(found["title"].startswith("Quick safety check"))
        self.assertEqual([option["label"] for option in found["options"]],
                         ["No, exit", "Yes, I trust this folder"])
        self.assertEqual(found["hint"], "Enter to confirm · Esc to cancel")
        self.assertFalse(found["numbers"])
        self.assertEqual(prompts.choice_keys(found, 1)[-2:], ["Down", "Enter"])


class NotAMenuTests(unittest.TestCase):
    def test_prose_that_mentions_hint_words_is_not_a_menu(self):
        """聊天里引用「←→ to switch」不能让它变成菜单，句子也不是菜单项。"""
        self.assertIsNone(prompts.parse_screen(CHAT_PROSE_QUOTING_HINTS))

    def test_options_ending_in_a_full_stop_are_not_menu_items(self):
        screen = ("  请问要怎么做\n"
                  "  Enter select · Esc cancel\n"
                  "  1. 先看一下这里的日志，然后再决定要不要重跑一遍。\n"
                  "  2. 直接把配置改掉，风险自负，出了问题再说。\n")
        self.assertIsNone(prompts.parse_screen(screen))

    def test_a_numbered_list_typed_in_chat_is_not_a_menu(self):
        """没有提示行的编号列表是正文，不是等待中的选择。"""
        self.assertIsNone(prompts.parse_screen(NUMBERED_CHAT_TEXT))


class NumberedTests(unittest.TestCase):
    def test_a_numbered_list_is_marked_as_numberable(self):
        found = prompts.parse_screen(COMPACT)
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "What would you like to do?")
        self.assertEqual([option["number"] for option in found["options"]], ["1", "2", "3"])
        self.assertEqual([option["label"] for option in found["options"]],
                         ["Compact and continue", "Start a new session", "Cancel"])
        self.assertTrue(found["numbers"])

    def test_cursor_plus_numbers_still_walks_with_arrows(self):
        """claude 是「❯ 1. Yes」：序号认得，但有光标时发方向键才可靠。"""
        found = prompts.parse_screen(CLAUDE_PROCEED)
        self.assertIsNotNone(found)
        self.assertTrue(found["numbers"])
        self.assertTrue(found["cursor"])
        self.assertEqual(len(found["options"]), 3)
        self.assertEqual(found["selected"], 0)
        self.assertEqual(found["title"], "Do you want to proceed?")
        keys = prompts.choice_keys(found, 2)
        self.assertNotIn("3", keys)
        self.assertEqual(keys[-2:], ["Down", "Down"] if False else ["Down", "Enter"])


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
