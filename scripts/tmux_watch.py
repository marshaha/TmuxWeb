"""Log every tmux client command that appears, to catch whoever kills a session.

A `tmux kill-session` client lives for a few milliseconds, so this polls /proc and records any
process whose command line mentions tmux. Polling costs CPU: the loop sleeps between passes and
gives up after `--seconds` (default 600). Run it only while investigating.

    ./.venv/bin/python scripts/tmux_watch.py --seconds 1800
"""
from __future__ import annotations

import argparse
import os
import time

LOG = "/tmp/tmux-watch.log"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=600, help="how long to watch")
    parser.add_argument("--interval", type=float, default=0.02, help="sleep between passes")
    parser.add_argument("--log", default=LOG)
    options = parser.parse_args()

    seen: set[str] = set()
    deadline = time.time() + options.seconds
    ignored = ("tmux-watch", "tmux_watch", "tmux_web", "run_service")
    with open(options.log, "a", buffering=1) as log:
        log.write(f"# watcher start {time.strftime('%F %T')} for {options.seconds}s\n")
        while time.time() < deadline:
            try:
                entries = os.listdir("/proc")
            except OSError:
                break
            for entry in entries:
                if not entry.isdigit() or entry in seen:
                    continue
                seen.add(entry)
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as handle:
                        raw = handle.read()
                except OSError:
                    continue
                if b"tmux" not in raw:
                    continue
                command = raw.replace(b"\0", b" ").decode(errors="replace").strip()
                if any(word in command for word in ignored):
                    continue
                try:
                    fields = open(f"/proc/{entry}/stat").read().split()
                    state, parent = fields[2], fields[3]
                except OSError:
                    state, parent = "?", "?"
                if state == "Z":
                    continue
                log.write(f"{time.strftime('%F %T')} pid={entry} ppid={parent} cmd={command[:400]}\n")
            time.sleep(options.interval)
    print(f"watcher done: {options.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
