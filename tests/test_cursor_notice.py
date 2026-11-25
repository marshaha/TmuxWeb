"""Read-only evidence from the physical tmux viewport."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import agent_prompts
from remote_hosts import cursor_frame_args

ROWS = ["Choose a provider?", "› 1. Local", "  2. Remote",
        "↑↓ navigate · Enter select", "╭─────────╮", "│ >       │", "╰─────────╯"]


def frame(rows=ROWS, x=0, y=1, visible=1, mode=0):
    meta = f"%4 123 80 {len(rows)} {x} {y} {visible} {mode}"
    return "\n".join([meta, *rows, meta, ""])


class CursorNoticeTests(unittest.TestCase):
    def test_cursor_corroborates_but_never_enables_answering(self):
        notice = agent_prompts.cursor_menu_notice(frame(), "%4", "123")
        self.assertEqual(notice["hint"], ROWS[0])
        self.assertTrue(notice["read_only"])
        self.assertEqual(notice["options"], [])
        changed = [ROWS[0], "  1. Local", "› 2. Remote", *ROWS[3:]]
        other = agent_prompts.cursor_menu_notice(frame(changed, y=2), "%4", "123")
        self.assertEqual(notice["revision"], other["revision"])
        inverse = [ROWS[0], "\x1b[7m1. Local\x1b[0m", *ROWS[2:]]
        self.assertIsNotNone(agent_prompts.cursor_menu_notice(frame(inverse), "%4", "123"))
        plain = [ROWS[0], "❯ Local", "  Remote", *ROWS[3:]]
        self.assertIsNotNone(agent_prompts.cursor_menu_notice(frame(plain), "%4", "123"))
        self.assertIsNone(agent_prompts.cursor_menu_notice(frame([ROWS[0], "❯ Local", "1. Log", *ROWS[3:]]), "%4", "123"))
        self.assertIsNotNone(agent_prompts.cursor_menu_notice(frame([*ROWS[4:], *ROWS[:4]], y=4), "%4", "123"))

    def test_hidden_stale_and_unrelated_frames_stay_silent(self):
        for sample in (frame(y=5, x=3), frame(y=0), frame(y=2, x=2), frame(x=60), frame(visible=0),
                       frame(mode=1), frame(ROWS[:-3]), frame(ROWS[:3]+["log output"]+ROWS[4:]),
                       frame([*ROWS[:4], "log output", *ROWS[4:]]),
                       frame([ROWS[0], "\x1b[38;5;7m1. Local", *ROWS[2:]]),
                       frame().replace("%4 123", "%4 222", 1),
                       frame().replace("↑↓ navigate", "autocomplete"),
                       frame().replace("│ >       │", "│ history │")):
            with self.subTest(sample=sample):
                self.assertIsNone(agent_prompts.cursor_menu_notice(sample, "%4", "123"))
        self.assertIsNone(agent_prompts.cursor_menu_notice(frame(), "%5", "123"))
        self.assertFalse(agent_prompts.possible_cursor_menu("╭───╮\n│ > history │\n╰───╯"))

    @unittest.skipUnless(shutil.which("tmux"), "tmux binary required for viewport capture")
    def test_tmux_snapshot_uses_visible_physical_rows(self):
        # This private socket has one drawing program; no terminal input is sent.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            program = path / "draw.py"
            screen = "\x1b[2J\x1b[H" + "\r\n".join(ROWS) + "\x1b[2;1H"
            returned = path / "return-cursor"
            program.write_text(
                f"import os,time\nos.write(1,{screen.encode()!r})\n"
                f"while not os.path.exists({str(returned)!r}): time.sleep(.02)\n"
                "os.write(1,b'\\x1b[6;4H')\ntime.sleep(30)\n")
            cmd = ["tmux", "-f", "/dev/null", "-S", str(path / "fixture.sock")]
            env = dict(os.environ, TMUX="", LC_ALL="C.UTF-8")
            def run(*args):
                return subprocess.check_output([*cmd, *args], text=True, env=env, timeout=5)
            try:
                pane, pid = run("new-session", "-dP", "-F", "#{pane_id} #{pane_pid}", "-x", "80",
                                "-y", "20", shlex.join([sys.executable, str(program)])).split()
                until = time.monotonic() + 4
                while time.monotonic() < until:
                    captured = run(*cursor_frame_args(pane))
                    if agent_prompts.cursor_menu_notice(captured, pane, pid):
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"could not locate the drawn selection in {captured!r}")
                returned.touch()
                until = time.monotonic() + 4
                while time.monotonic() < until:
                    captured = run(*cursor_frame_args(pane))
                    if captured.splitlines()[0].split()[4:6] == ["3", "5"]:
                        self.assertIsNone(agent_prompts.cursor_menu_notice(captured, pane, pid))
                        break
                    time.sleep(0.05)
                else:
                    self.fail("fixture cursor did not return to editor")
            finally:
                subprocess.run([*cmd, "kill-server"], env=env, capture_output=True, timeout=5)
