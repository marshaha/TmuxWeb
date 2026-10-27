#!/usr/bin/env python3
"""A deliberately dumb stand-in for an agent CLI, for relay verification.

Agent CLIs run the terminal in raw mode, so this does too: it echoes what it
receives, redraws an ``❯`` prompt, and answers any TmuxWeb relay request whose
marker it has seen once a submit (C-m / Enter) arrives. It never calls a model;
it exists so the relay path can be exercised end to end in TESTING.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import termios
import tty

PROMPT = "❯ "
RELAY_RE = re.compile(r"TMUXWEB_RELAY[^\]]*\]")
MARKER_RE = re.compile(r"TMUXWEB_REPLY_([0-9a-f]{24}):")


def write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def reply(lines: list[str]) -> str | None:
    joined = "\n".join(lines)
    marker = MARKER_RE.search(joined)
    if not marker:
        return None
    question = ""
    for line in lines:
        candidate = line.strip()
        if not candidate or RELAY_RE.search(candidate) or MARKER_RE.search(candidate):
            continue
        question = candidate
        break
    return "TMUXWEB_REPLY_%s: %s" % (
        marker.group(1), json.dumps("fake answer to: " + question, ensure_ascii=False)
    )


def main() -> None:
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    lines: list[str] = [""]
    try:
        tty.setraw(fd)
        write("fake agent ready (type a relay prompt)\r\n" + PROMPT)
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            for raw in chunk.split(b"\x00"):
                for byte in raw.decode(errors="replace"):
                    if byte in "\r\n":
                        write("\r\n")
                        answer = reply(lines)
                        if answer:
                            lines = [""]
                            write(answer + "\r\n" + PROMPT)
                        elif lines[-1] != "":
                            lines.append("")
                        continue
                    if byte == "\x15":  # Ctrl-U clears the current draft
                        lines = [""]
                        write("\r" + PROMPT)
                        continue
                    if byte in "\x7f\x08":
                        if lines[-1]:
                            lines[-1] = lines[-1][:-1]
                            write("\b \b")
                        continue
                    lines[-1] += byte
                    write(byte)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    main()
