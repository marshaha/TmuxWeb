"""The "needs you" gate: a parsed menu alone must NEVER fire.

Pure screen parsing misjudges — the menu-like text in a streaming transcript,
a stale prompt left on screen after the turn moved on, and a transient render
mid-stream all parse as menus. So ``needs_input`` is a conjunction of three
independent legs, and the gate fails closed: if any leg is unavailable for a
provider, the pane simply never raises ``needs_input``.

    1. decision point — the agent's own session file says it is at a
                       decision point: the turn ended (agent_status IDLE) OR
                       a tool call is outstanding (issued, no result yet).
                       A merely-executing turn can never fire.
    2. screen stable — the same menu parses identically on consecutive
                       observations; a transient render never fires.
    3. menu present  — agent_prompts.parse_screen returns structured options.

Each leg is tested alone below: any single signal firing on its own is a bug.
"""
import tempfile
import unittest
from pathlib import Path

import agent_prompts as prompts
import needs_input
from needs_input import NeedsInputGate

# claude 的权限确认（test_agent_prompts.CLAUDE_PROCEED 的同款结构）
MENU = """
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again this session
   3. No, and tell Claude what to do differently
 Esc to cancel
"""

# 同一个菜单，但光标移到第二项：解析结果不同，稳定计数必须重来
MENU_CURSOR_MOVED = """
 Do you want to proceed?
   1. Yes
 ❯ 2. Yes, and don't ask again this session
   3. No, and tell Claude what to do differently
 Esc to cancel
"""

# 聊天正文里的编号列表：parse_screen 本来就不认（没有提示行），但就算哪天认了，
# 没有 turn-ended 这一腿也绝不能亮灯
MENU_LIKE_CHAT = """
 回一句是哪个就行。
 ❯ 1. 是你之前的弹窗选择
 2. 请你把粘贴板的图片直接转附件上传
"""

IDLE = True
RUNNING = False  # not a decision point: merely executing


class GateConjunctionTests(unittest.TestCase):
    def test_menu_alone_does_not_fire(self):
        """只有菜单、agent 只是在执行：最常见的误判 —— 流式输出里菜单一闪而过。"""
        gate = NeedsInputGate()
        for _ in range(3):
            self.assertIsNone(gate.observe("%4", decision_point=RUNNING, screen_text=MENU))

    def test_idle_alone_does_not_fire(self):
        """没有决策点、屏幕上没有菜单：空闲 pane 没有什么要你答的。"""
        gate = NeedsInputGate()
        for _ in range(3):
            self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text="idle\n"))

    def test_stable_screen_alone_does_not_fire(self):
        """屏幕稳定、但 agent 只是在执行：stale prompt —— 上一轮的菜单还留在屏上。"""
        gate = NeedsInputGate()
        for _ in range(3):
            self.assertIsNone(gate.observe("%4", decision_point=RUNNING, screen_text=MENU))

    def test_all_three_legs_together_fire(self):
        """三腿齐了才亮灯：第二轮相同观测才算稳定，第一轮只记不亮。"""
        gate = NeedsInputGate()
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))
        found = gate.observe("%4", decision_point=IDLE, screen_text=MENU)
        self.assertIsNotNone(found)
        self.assertEqual([option["label"] for option in found["options"]],
                         ["Yes", "Yes, and don't ask again this session",
                          "No, and tell Claude what to do differently"])
        # 亮着的时候每轮都带 prompt，/api/panes/status 不用等“变化边沿”
        self.assertIsNotNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))

    def test_a_transient_menu_that_changes_between_polls_does_not_fire(self):
        """菜单在两轮观测之间变了（流式重绘/光标移动）：稳定计数重来。"""
        gate = NeedsInputGate()
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU_CURSOR_MOVED))
        # 变完之后再稳两轮才亮：第二轮相同观测就是稳定的第二票
        self.assertIsNotNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU_CURSOR_MOVED))

    def test_a_stale_menu_while_the_turn_is_still_running_does_not_fire(self):
        """菜单一直都在，但没有决策点：上一轮的确认框还留在屏上。"""
        gate = NeedsInputGate()
        for _ in range(3):
            self.assertIsNone(gate.observe("%4", decision_point=RUNNING, screen_text=MENU))
        # 到决策点之后还要再稳两轮，不能拿执行时攒下的“稳定”直接亮灯
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))
        self.assertIsNotNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))

    def test_the_gate_resets_when_the_menu_goes_away(self):
        """菜单没了灯就灭：答完之后下一轮不再带 prompt。"""
        gate = NeedsInputGate()
        gate.observe("%4", decision_point=IDLE, screen_text=MENU)
        self.assertIsNotNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text="idle\n"))
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))

    def test_an_unknown_turn_state_never_fires(self):
        """没有 session-file 决策信号的 provider（hermes / shell / 未知）：fail closed。"""
        gate = NeedsInputGate()
        for _ in range(3):
            self.assertIsNone(gate.observe("%4", decision_point=None, screen_text=MENU))

    def test_panes_are_tracked_independently(self):
        """一个 pane 亮灯不能把另一个 pane 也点亮。"""
        gate = NeedsInputGate()
        gate.observe("%4", decision_point=IDLE, screen_text=MENU)
        self.assertIsNotNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))
        self.assertIsNone(gate.observe("%5", decision_point=IDLE, screen_text=MENU))
        self.assertIsNotNone(gate.observe("%5", decision_point=IDLE, screen_text=MENU))

    def test_prune_forgets_vanished_panes(self):
        """pane 消失后它的稳定计数要清掉：%N 会被新 pane 复用，不能继承旧灯。"""
        gate = NeedsInputGate()
        gate.observe("%4", decision_point=IDLE, screen_text=MENU)
        gate.prune({"%9"})
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=MENU))


class PromptSignatureTests(unittest.TestCase):
    def test_the_same_menu_has_the_same_signature(self):
        first = prompts.parse_screen(MENU)
        second = prompts.parse_screen(MENU)
        self.assertEqual(needs_input.prompt_signature(first),
                         needs_input.prompt_signature(second))

    def test_a_moved_cursor_is_a_different_menu(self):
        self.assertNotEqual(needs_input.prompt_signature(prompts.parse_screen(MENU)),
                            needs_input.prompt_signature(prompts.parse_screen(MENU_CURSOR_MOVED)))

    def test_no_menu_has_no_signature(self):
        self.assertIsNone(needs_input.prompt_signature(None))
        self.assertIsNone(needs_input.prompt_signature(prompts.parse_screen("idle\n")))


class EnrichPromptTests(unittest.TestCase):
    def test_each_option_carries_the_keys_the_console_sends(self):
        """前端只负责照单发键：按键序列由服务端一次算好，随 prompt 下发。"""
        found = needs_input.enrich_prompt(prompts.parse_screen(MENU))
        self.assertIsNotNone(found)
        keys = [option["keys"] for option in found["options"]]
        self.assertEqual(keys[0][-1], "Enter")
        self.assertEqual(keys[2][-2:], ["Down", "Enter"])
        # 每个选项的 keys 都只能是 /api/panes/{id}/key 接受的键
        import tmux_web
        allowed = tmux_web.ALLOWED_KEYS
        for sequence in keys:
            for key in sequence:
                self.assertIn(key, allowed, key)

    def test_a_confirm_menu_maps_yes_and_no_to_single_keys(self):
        found = needs_input.enrich_prompt(
            prompts.parse_screen('  Overwrite "a.txt"? [y/N]\n'))
        self.assertEqual(found["options"][0]["keys"], ["y", "Enter"])
        self.assertEqual(found["options"][1]["keys"], ["n", "Enter"])

    def test_enriching_never_mutates_the_parsed_prompt(self):
        found = prompts.parse_screen(MENU)
        enriched = needs_input.enrich_prompt(found)
        self.assertNotIn("keys", found["options"][0])
        self.assertIn("keys", enriched["options"][0])
        self.assertIsNone(needs_input.enrich_prompt(None))

    def test_enriching_keeps_descriptions_and_the_recommended_mark(self):
        """描述行与首选项标记随 prompt 下发，前端照单渲染，不用再算。"""
        menu = """
 Which approach should I use?

 ❯ Quick fix
     Patch the bug in place
   Full refactor
     Restructure the module

 Enter select · Esc cancel
"""
        found = needs_input.enrich_prompt(prompts.parse_screen(menu))
        self.assertIsNotNone(found)
        self.assertEqual(found["options"][0]["description"], "Patch the bug in place")
        self.assertEqual(found["options"][1]["description"], "Restructure the module")
        self.assertIs(found["options"][0].get("recommended"), True)
        self.assertNotIn("recommended", found["options"][1])
        self.assertEqual(found["options"][0]["keys"][-1], "Enter")

    def test_a_description_only_change_does_not_reset_stability(self):
        """描述只是展示文本：两轮观测之间只有描述换行重排，稳定计数不能重来，
        否则卡片永远亮不起来。"""
        first = """
 Which approach should I use?

 ❯ Quick fix
     Patch the bug in place
   Full refactor
     Restructure the module

 Enter select · Esc cancel
"""
        second = first.replace("Patch the bug in place", "Patch it right here")
        self.assertEqual(needs_input.prompt_signature(prompts.parse_screen(first)),
                         needs_input.prompt_signature(prompts.parse_screen(second)))
        gate = NeedsInputGate()
        self.assertIsNone(gate.observe("%4", decision_point=IDLE, screen_text=first))
        found = gate.observe("%4", decision_point=IDLE, screen_text=second)
        self.assertIsNotNone(found, "只有描述变了，第二轮就该亮灯")
        self.assertEqual(found["options"][0]["description"], "Patch it right here")


if __name__ == "__main__":
    unittest.main()


class DecisionPointCompositionTests(unittest.TestCase):
    """Leg 1, corrected, composed with legs 2 and 3: the decision point comes
    from a real session file (agent_status), the gate does the rest.

    The case the first version of this gate got wrong: a permission wait. The
    turn-end marker never fires there (0/4008 in real Claude files), so the
    file's verdict is BUSY while the agent sits at the prompt — only the
    outstanding tool call says it is waiting.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _claude_tail(self, records):
        import agent_status
        import json as _json
        path = self.root / "claude.jsonl"
        path.write_text("\n".join(_json.dumps(record) for record in records) + "\n")
        return agent_status.decision_point_for_file("claude", path)

    def test_a_permission_wait_fires(self):
        """assistant 发了 tool_use、结果还没回、屏幕稳稳停在确认菜单上：亮灯。"""
        from tests.test_agent_status import claude_assistant
        decision = self._claude_tail([{"type": "user"}, claude_assistant("toolu_1")])
        self.assertTrue(decision, "verdict 是 BUSY，但调用 outstanding，仍是决策点")
        gate = NeedsInputGate()
        self.assertIsNone(gate.observe("%4", decision_point=decision, screen_text=MENU))
        found = gate.observe("%4", decision_point=decision, screen_text=MENU)
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "Do you want to proceed?")

    def test_the_same_wait_with_a_changing_screen_does_not_fire(self):
        """同一个等待，但屏幕还在变（流式重绘）：leg 2 不过，绝不亮。"""
        from tests.test_agent_status import claude_assistant
        decision = self._claude_tail([{"type": "user"}, claude_assistant("toolu_1")])
        gate = NeedsInputGate()
        # 屏幕每轮都在变（光标来回跳）：稳定计数永远攒不到第二票
        for screen in (MENU, MENU_CURSOR_MOVED, MENU):
            self.assertIsNone(gate.observe("%4", decision_point=decision, screen_text=screen))
        # 停住不动之后，两轮就亮
        self.assertIsNone(gate.observe("%4", decision_point=decision,
                                       screen_text=MENU_CURSOR_MOVED))
        self.assertIsNotNone(gate.observe("%4", decision_point=decision,
                                          screen_text=MENU_CURSOR_MOVED))

    def test_a_finished_turn_with_a_stable_menu_still_fires(self):
        """旧的 leg-1 情形照样亮：turn 结束、菜单稳定。"""
        decision = self._claude_tail([{"type": "user"},
                                      {"type": "system", "subtype": "turn_duration"}])
        self.assertTrue(decision)
        gate = NeedsInputGate()
        self.assertIsNone(gate.observe("%4", decision_point=decision, screen_text=MENU))
        self.assertIsNotNone(gate.observe("%4", decision_point=decision, screen_text=MENU))

    def test_an_executing_tool_with_no_menu_does_not_fire(self):
        """调用 outstanding，但屏幕上没有菜单（工具正在跑）：leg 3 不过。"""
        from tests.test_agent_status import claude_assistant
        decision = self._claude_tail([{"type": "user"}, claude_assistant("toolu_1")])
        self.assertTrue(decision)
        gate = NeedsInputGate()
        for _ in range(3):
            self.assertIsNone(gate.observe(
                "%4", decision_point=decision,
                screen_text="running ls -la\nsome output here\n"))
