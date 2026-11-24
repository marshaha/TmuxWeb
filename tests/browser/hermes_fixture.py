"""An offline two-question Hermes screen for the browser/PTY regression test."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tty


def main(result):
    questions = [
        ("provider", "选择 provider？", ["openai", "claude"]),
        ("model", "选择模型？", ["默认模型", "其他模型"]),
    ]
    def hook(event):
        receiver = Path(__file__).resolve().parents[2] / "plugins/agent/hooks/observe.py"
        payload = {"session_id": "offline-session", "hook_event_name": event, "extra": {
            "kind": "clarify", "platform": "tui", "request_id": "offline-request",
            "prompt": "\n".join(q[1] for q in questions)}}
        subprocess.run([sys.executable, str(receiver), "hermes"], input=json.dumps(payload),
                       text=True, check=True, timeout=5)
    opened = False
    active = 0
    answers = {}
    tty.setraw(sys.stdin.fileno())
    while len(answers) < len(questions):
        rows = ["2 questions"]
        for index, (name, title, options) in enumerate(questions):
            marker = "✓" if name in answers else "▸" if index == active else "·"
            rows.append(f"{marker} {title}")
            if name in answers:
                rows.append("    " + answers[name])
            if index == active:
                for number, label in enumerate([*options, "Other (type your answer)"], 1):
                    cursor = "❯" if number == 1 else " "
                    rows.append(f"  {cursor} {number}. {label}")
        screen = ["╭─ Hermes Agent needs your input ─╮", *[f"│ {row} │" for row in rows],
                  "╰─────────────────────────────────╯", "↑/↓ to select, Enter to lock, Tab next question",
                  "── ❓ ──", "? ❯", "────", "Hermes"]
        print("\x1b[2J\x1b[H" + "\r\n".join(screen), end="", flush=True)
        if not opened:
            while not result.with_suffix(".allow-hook").exists():
                time.sleep(.05)
            hook("on_human_input_request")
            result.with_suffix(".ready").touch()
            opened = True
        key = os.read(0, 1)
        if key == b"\x1b":
            key += os.read(0, 1) + os.read(0, 1)
        if key in (b"\t", b"\x1b[Z"):
            active = (active + (1 if key == b"\t" else -1)) % len(questions)
        elif key in (b"1", b"2"):
            name, _, options = questions[active]
            answers[name] = options[int(key) - 1]
            active = next((i for i, q in enumerate(questions) if q[0] not in answers), active)
        else:
            raise ValueError(f"unexpected input {key!r}")
    hook("on_human_input_resolved")
    result.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
