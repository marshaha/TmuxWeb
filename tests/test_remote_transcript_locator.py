"""远程 transcript 定位：pane 自己进程里的会话 id 必须优先于 cwd/标题猜测。

真实故障（oracle 的 claude:0）：同一个 cwd 里同时跑着好几个 Claude 会话，脚本按
"最新的文件 + 标题/屏幕探测" 选文件，选中了隔壁 pane 的会话，于是网页里的 chat 一直
停在另一段对话上，看起来像"不刷新"。
"""

import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
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

    def test_the_hermes_anchor_is_the_agent_process_not_the_pane_shell(self):
        """macmini #39：壳 zsh 活了 30 天，hermes 25 分钟前重启过；按壳的启动时间
        配会落到 10 月 10 日的旧会话，按 agent 的启动时间配才落到新会话。"""
        hermes = SCRIPT.split("# hermes: prefer the per-session jsonl", 1)[1]
        self.assertIn('anchor_pid=$(hermes_agent_pid "$pane_pid")', hermes)
        self.assertIn('ps -o etime= -p "$anchor_pid"', hermes)
        self.assertIn('[ -n "$anchor_pid" ] || anchor_pid=$pane_pid', hermes)
        # 壳的 etime 不能再直接当锚点
        self.assertNotIn('ps -o etime= -p "$pane_pid"', hermes)

    def test_hermes_agent_pid_finds_the_cli_and_skips_the_gateway(self):
        helper = SCRIPT.split("hermes_agent_pid() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("hermes_cli.main", helper)
        self.assertIn("gateway", helper)
        self.assertIn("pgrep -P", helper)
        # macOS 远端没有 /proc，必须用 ps
        self.assertIn("ps -o args=", helper)
        self.assertNotIn("/proc/", helper)


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

    AGENT_ARGS = ("/Users/marscrazy/.hermes/tools/python-3.14.9/bin/python3 -I -c "
                  "from hermes_cli.main import main --provider axera -m deepseek-flash")
    GATEWAY_ARGS = ("/Users/marscrazy/.hermes/tools/python-3.14.9/bin/python3 -m "
                    "hermes_cli.main --profile default gateway run --quiet --idle-exit")

    def run_agent_pid(self, children, args_lines, root="5446"):
        """Run the real hermes_agent_pid helper against stubbed ps/pgrep.

        children: {pid: [child pids]}; args_lines: {pid: ps args line}.
        """
        body = SCRIPT.split("hermes_agent_pid() {", 1)[1].split("\n}\n", 1)[0]
        pgrep_arms = "".join(
            "    %s) printf '%%s\\n' '%s'; ;;\n" % (pid, " ".join(str(c) for c in kids)) if kids
            else "    %s) return 1 ;;\n" % pid
            for pid, kids in children.items()
        )
        ps_arms = "".join(
            "    %s) printf '%%s\\n' %s; ;;\n" % (pid, repr(line))
            for pid, line in args_lines.items()
        )
        script = (
            "set -u\n"
            "pgrep() { case \"$2\" in\n" + pgrep_arms + "    *) return 1 ;;\n  esac; }\n"
            "ps() { case \"$4\" in\n" + ps_arms + "    *) printf '%s\\n' '' ;;\n  esac; }\n"
            "hermes_agent_pid() {" + body + "\n}\n"
            "hermes_agent_pid %s || true\n" % root
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_agent_pid_finds_the_agent_child_of_an_old_shell(self):
        self.assertEqual(
            self.run_agent_pid({5446: [38059], 38059: []},
                               {5446: "-zsh", 38059: self.AGENT_ARGS}),
            "38059")

    def test_agent_pid_is_empty_when_no_agent_process_exists(self):
        self.assertEqual(
            self.run_agent_pid({5446: []}, {5446: "-zsh"}), "")

    def test_agent_pid_skips_the_gateway(self):
        self.assertEqual(
            self.run_agent_pid({5446: [43104], 43104: []},
                               {5446: "-zsh", 43104: self.GATEWAY_ARGS}),
            "")

    def test_agent_pid_finds_the_agent_behind_a_launcher_shell(self):
        wrapper = "zsh -c /Users/marscrazy/.local/bin/hermes --provider axera -m deepseek-flash"
        self.assertEqual(
            self.run_agent_pid({5446: [35791], 35791: [35792], 35792: []},
                               {5446: "-zsh", 35791: wrapper, 35792: self.AGENT_ARGS}),
            "35792")

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



@unittest.skipUnless(shutil.which("bash"), "bash is required to exercise the shell helper")
@unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 is required for the hermes state.db fixture")
class HermesAnchorEndToEndTests(unittest.TestCase):
    """壳老、agent 新时，发现脚本必须挑新会话（macmini #39 的真故障）。

    壳 zsh 活了 30 天（etime 30-18:33:53），hermes 25 分钟前重启过
    （etime 25:37）：按壳的启动时间配会落到 10 月 10 日的旧会话（4 条），
    按 agent 的启动时间配才落到今天的新会话。agent 退出、树里找不到它时，
    回落到 pane 的启动时间（旧行为）。
    """

    OLD_ID = "20261010_183501_df9c7f"
    NEW_ID = "local-aff8d567test"
    AGENT_ARGS = ("/Users/marscrazy/.hermes/tools/python-3.14.9/bin/python3 -I -c "
                  "from hermes_cli.main import main --provider axera -m deepseek-flash")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.cwd = self.home / "workspace" / "hermes"
        self.cwd.mkdir(parents=True)
        self.bindir = base / "bin"
        self.bindir.mkdir()
        self._write_stub("tmux", (
            "#!/bin/sh\n"
            "case \"$5\" in\n"
            "  *pane_current_path*) printf '%s<::>%s<::>%s\\n' \"$TMUXWEB_TEST_CWD\" \"hermes\" \"python3.14\" ;;\n"
            "  *pane_pid*) printf '%s\\n' \"5446\" ;;\n"
            "  *pane_title*) printf '%s\\n' \"hermes\" ;;\n"
            "esac\n"
        ))
        self._write_stub("ps", (
            "#!/bin/sh\n"
            "if [ \"$2\" = \"etime=\" ]; then\n"
            "  case \"$4\" in\n"
            "    5446) printf '%s\\n' \"30-18:33:53\" ;;\n"
            "    38059) printf '%s\\n' \"25:37\" ;;\n"
            "    *) printf '%s\\n' \"\" ;;\n"
            "  esac\n"
            "else\n"
            "  case \"$4\" in\n"
            "    5446) printf '%s\\n' \"-zsh\" ;;\n"
            "    38059) printf '%s\\n' \"" + self.AGENT_ARGS + "\" ;;\n"
            "    *) printf '%s\\n' \"\" ;;\n"
            "  esac\n"
            "fi\n"
        ))
        self._write_stub("pgrep", (
            "#!/bin/sh\n"
            "case \"$2\" in\n"
            "  5446) printf '%s\\n' \"38059\" ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        ))
        now = time.time()
        (self.home / ".hermes").mkdir(parents=True)
        con = sqlite3.connect(self.home / ".hermes" / "state.db")
        con.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, started_at REAL,"
                    " last_activity_at REAL, model TEXT)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,"
                    " role TEXT, content TEXT, timestamp REAL, active INTEGER)")
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                    (self.OLD_ID, str(self.cwd), now - 38 * 86400, now - 38 * 86400, "deepseek-flash"))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                    (self.NEW_ID, str(self.cwd), now - 1500, now - 60, "deepseek-flash"))
        for i in range(4):
            con.execute("INSERT INTO messages (session_id, role, content, timestamp, active)"
                        " VALUES (?,?,?,?,1)",
                        (self.OLD_ID, "user" if i % 2 == 0 else "assistant",
                         "old %d" % i, now - 38 * 86400 + i))
        for i in range(3):
            con.execute("INSERT INTO messages (session_id, role, content, timestamp, active)"
                        " VALUES (?,?,?,?,1)",
                        (self.NEW_ID, "user" if i % 2 == 0 else "assistant",
                         "new %d" % i, now - 1400 + i))
        con.commit()
        con.close()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "TMUX_BIN": str(self.bindir / "tmux"),
            "TMUXWEB_TEST_CWD": str(self.cwd),
            "PATH": str(self.bindir) + ":/usr/bin:/bin",
        }

    def _write_stub(self, name, text):
        path = self.bindir / name
        path.write_text(text)
        path.chmod(0o755)

    def run_discovery(self):
        result = subprocess.run(["sh", "-c", SCRIPT, "sh", "%143", "20"],
                                capture_output=True, env=self.env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        head, _, tail = result.stdout.partition(b"TMUXWEB_PAYLOAD\n")
        header = dict(line.split("<::>", 1) for line in head.decode().splitlines()
                      if "<::>" in line)
        return header, gzip.decompress(tail)

    def test_picks_the_restarted_agent_session_not_the_old_one(self):
        header, payload = self.run_discovery()
        self.assertEqual(header["TMUXWEB_SESSION"], self.NEW_ID)
        rows = json.loads(payload.decode())
        self.assertEqual([row["content"] for row in rows], ["new 2", "new 1", "new 0"])

    def test_falls_back_to_the_pane_shell_when_the_agent_is_gone(self):
        self._write_stub("pgrep", "#!/bin/sh\nexit 1\n")
        header, payload = self.run_discovery()
        self.assertEqual(header["TMUXWEB_SESSION"], self.OLD_ID)


if __name__ == "__main__":
    unittest.main()
