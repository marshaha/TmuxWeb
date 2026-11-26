#!/usr/bin/env python3
"""Report working, blocked, done or idle from the script's own tmux pane."""
import argparse
import os
import re
import subprocess
import sys
import time


STATES = ("working", "blocked", "done", "idle", "error", "clear")


def osc_state(frame):
    """Decode one root-program OSC 7501 record provided by a cooperating hook."""
    match = re.fullmatch(rb"\x1b\]7501;([ -~]{1,4096})(?:\x07|\x1b\\)", frame)
    if not match:
        return None
    fields = {}
    for field in match[1].decode("ascii").split(":"):
        key, sep, value = field.partition("=")
        if not sep or not key or key in fields:
            return None
        fields[key] = value
    state = fields.get("state")
    return state if state in STATES and not fields.get("id") else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("state", nargs="?", choices=STATES)
    group.add_argument("--osc", action="store_true", help="read one complete OSC 7501 root report from stdin")
    args = parser.parse_args()
    state = osc_state(sys.stdin.buffer.read(4110)) if args.osc else args.state
    if not state:
        parser.error("invalid OSC 7501 root status report")
    pane = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%[0-9]+", pane) or not os.environ.get("TMUX"):
        parser.error("run inside a tmux pane")
    binary = os.environ.get("TMUX_BIN") or "tmux"
    pid = subprocess.check_output([binary, "display-message", "-pt", pane, "#{pane_pid}"],
                                  text=True, timeout=3).strip()
    if not pid.isdecimal():
        parser.error("pane no longer exists")
    value = "" if state == "clear" else f"{pid}:{time.time_ns()//1_000_000}:{state}"
    subprocess.run([binary, "set-option", "-pt", pane, "@tmuxweb_status", value], check=True, timeout=3)


if __name__ == "__main__":
    main()
