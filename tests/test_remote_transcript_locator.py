"""远程 transcript 定位：pane 自己进程里的会话 id 必须优先于 cwd/标题猜测。

真实故障（oracle 的 claude:0）：同一个 cwd 里同时跑着好几个 Claude 会话，脚本按
"最新的文件 + 标题/屏幕探测" 选文件，选中了隔壁 pane 的会话，于是网页里的 chat 一直
停在另一段对话上，看起来像"不刷新"。
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import remote_hosts

MATCH = re.search(
    r'REMOTE_TRANSCRIPT_SCRIPT = .*?r"""(.*?)"""',
    Path(remote_hosts.__file__).read_text(),
    re.S,
)
assert MATCH is not None, "REMOTE_TRANSCRIPT_SCRIPT not found"
SCRIPT = MATCH.group(1)


class RemoteScriptShapeTests(unittest.TestCase):
    def test_the_claude_session_comes_from_the_pane_process_first(self):
        claude = SCRIPT.split('if [ "$provider" = claude ]; then', 1)[1].split('if [ "$provider" = codex ]; then', 1)[0]
        self.assertIn('hint=$(session_hint "$pane_pid")', claude)
        # 先按 id 精确定位，找不到才走标题/屏幕探测
        self.assertLess(claude.index('hint=$(session_hint'), claude.index('$title'))
        self.assertIn('exact=$(ls "$HOME"/.claude/projects/*/"$hint".jsonl', claude)
        # 探测只在没有精确命中时发生
        self.assertIn('[ -z "$file" ] && [ -n "$title" ]', claude)

    def test_the_hint_supports_both_flags_and_does_not_need_proc(self):
        helper = SCRIPT.split("session_hint() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("--resume", helper)
        self.assertIn("--session-id", helper)
        # macOS 远端没有 /proc，必须用 ps
        self.assertIn("ps -o args=", helper)
        self.assertNotIn("/proc/", helper)

    def test_the_screen_probe_scores_candidates_instead_of_first_match(self):
        probes = SCRIPT.split("if [ -z \"$file" + "\" ]; then", 1)[1].split("$candidates | head -1", 1)[0]
        self.assertIn("best_score", probes)
        self.assertIn("grep -c -F -f", probes)
        self.assertNotIn("break 2", probes)

    def test_the_codex_session_comes_from_the_pane_process_first(self):
        codex = SCRIPT.split('if [ "$provider" = codex ]; then', 1)[1].split('# kimi', 1)[0]
        self.assertIn('hint=$(session_hint "$pane_pid")', codex)
        self.assertLess(codex.index('hint=$(session_hint'), codex.index('session_index.jsonl'))
        self.assertIn('exact=$(find "$HOME/.codex/sessions" -name "*$hint*.jsonl"', codex)
        # 候选按写入时间排序，别用 find 的目录顺序
        self.assertIn("candidates=$(ls -t $candidates", codex)

    def test_the_codex_title_index_matches_compact_json(self):
        """`{"id":"…","thread_name":"…"}` has no space after the colon; only the spaced
        form used to be grepped, which silently disabled the index and left the cwd guess."""
        codex = SCRIPT.split('if [ "$provider" = codex ]; then', 1)[1].split('# kimi', 1)[0]
        self.assertIn('thread_name\\":\\"$short', codex)     # compact: "thread_name":"…"
        self.assertIn('thread_name\\": \\"$short', codex)    # spaced:  "thread_name": "…"

    def test_hermes_reuses_the_same_helper(self):
        hermes = SCRIPT.split("# hermes: prefer the per-session jsonl", 1)[1]
        self.assertIn('sid=$(session_hint "$pane_pid")', hermes)


@unittest.skipUnless(shutil.which("bash"), "bash is required to exercise the shell helper")
class SessionHintShellTests(unittest.TestCase):
    """Run the real helper against a stubbed ``ps``/``pgrep``."""

    def run_hint(self, args_line: str) -> str:
        helper = SCRIPT.split("session_hint() {", 1)[0].split("error() {", 1)[1]
        body = SCRIPT.split("session_hint() {", 1)[1].split("\n}\n", 1)[0]
        script = (
            "set -u\n"
            "error() { :; }\n"
            "pgrep() { return 1; }\n"
            f"ps() {{ printf '%s\\n' {args_line!r}; }}\n"
            "session_hint() {" + body + "\n}\n"
            "session_hint 4242 || true\n"
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_a_resumed_session_id_is_read_from_argv(self):
        self.assertEqual(self.run_hint("claude --resume 11111111-2222-3333-4444-555555555555"),
                         "11111111-2222-3333-4444-555555555555")

    def test_the_session_id_flag_is_understood_too(self):
        self.assertEqual(self.run_hint("claude --session-id abc123def456"), "abc123def456")

    def test_the_codex_resume_subcommand_is_understood(self):
        self.assertEqual(self.run_hint("node /usr/local/bin/codex resume 0199aaaa-bbbb-cccc-dddd-eeeeffff0000"),
                         "0199aaaa-bbbb-cccc-dddd-eeeeffff0000")

    def test_codex_resume_last_yields_no_hint(self):
        self.assertEqual(self.run_hint("codex resume --last"), "")

    def test_the_codex_index_lookup_finds_a_compact_entry(self):
        """Run the same grep+sed against a codex-shaped index file."""
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "session_index.jsonl"
            index.write_text(
                '{"id":"01a13343-d2fd-7741-bdae-d8316da58002","thread_name":"核对 DQ_RX_DLY 位域"}\n'
                '{"id":"01a1415f-93a2-7c63-8899-dec97e3e465f","thread_name":"读取并执行任务"}\n'
            )
            script = f"""
short='读取并执行任务'
id=$(grep -F -e "\\"thread_name\\":\\"$short\\"" -e "\\"thread_name\\": \\"$short\\"" '{index}' 2>/dev/null \
  | tail -1 | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')
printf '%s' "$id"
"""
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.stdout.strip(), "01a1415f-93a2-7c63-8899-dec97e3e465f")



if __name__ == "__main__":
    unittest.main()
