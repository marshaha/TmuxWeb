#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

KINDS = {"claude", "codex", "hermes"}
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PANE_RE = re.compile(r"^%[0-9]+$")


class TmuxChatError(RuntimeError):
    pass


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError("invalid registry")
        return data

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, self.path)

    def register(self, alias: str, pane: str, kind: str) -> None:
        if not ALIAS_RE.fullmatch(alias):
            raise ValueError("alias must contain only letters, digits, '_' or '-'")
        if not PANE_RE.fullmatch(pane):
            raise ValueError("pane must be an exact tmux pane id such as %4")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(KINDS))}")
        data = self._read()
        data[alias] = {"pane": pane, "kind": kind}
        self._write(data)

    def resolve(self, alias: str) -> dict[str, str]:
        try:
            return self._read()[alias]
        except KeyError as exc:
            raise ValueError(f"unknown alias: {alias}") from exc

    def list(self) -> dict[str, dict[str, str]]:
        return self._read()


def run_tmux(*args: str, input_bytes: bytes | None = None) -> str:
    completed = subprocess.run(
        ["tmux", *args], input=input_bytes, capture_output=True, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise TmuxChatError(detail or f"tmux {' '.join(args)} failed")
    return completed.stdout.decode(errors="replace")


def pane_info(pane: str) -> dict[str, str]:
    raw = run_tmux(
        "display-message", "-p", "-t", pane,
        "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}",
    ).rstrip("\n")
    pane_id, target, command, path = raw.split("\t", 3)
    if pane_id != pane:
        raise TmuxChatError(f"pane id changed: expected {pane}, got {pane_id}")
    return {"pane": pane_id, "target": target, "command": command, "path": path}


def capture_pane(pane: str, history: int = 200) -> str:
    return run_tmux("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{history}")


def pane_is_ready(kind: str, text: str) -> bool:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-12:])
    if kind == "codex":
        return "› Ask Codex to do anything" in tail or bool(re.search(r"(?m)^›\s*$", tail))
    if kind in {"claude", "hermes"}:
        return bool(re.search(r"(?m)^❯(?:\s|\u00a0).*$", tail))
    return False


def extract_reply(text: str, token: str) -> str | None:
    prefix = f"TMUX_BRIDGE_REPLY_{token}:"
    decoder = json.JSONDecoder()
    found: str | None = None
    for line in text.splitlines():
        position = line.find(prefix)
        if position < 0:
            continue
        candidate = line[position + len(prefix):].strip()
        try:
            value, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            found = value
        elif isinstance(value, dict):
            for key in ("message", "answer", "reply", "response", "result"):
                if isinstance(value.get(key), str):
                    found = value[key]
                    break
    return found


def paste_and_submit(pane: str, text: str, *, submit_delay: float = 0.0) -> None:
    buffer_name = f"tmux-chat-{secrets.token_hex(6)}"
    try:
        run_tmux("load-buffer", "-b", buffer_name, "-", input_bytes=text.encode())
        run_tmux("paste-buffer", "-b", buffer_name, "-d", "-t", pane)
        if submit_delay:
            time.sleep(submit_delay)
        run_tmux("send-keys", "-t", pane, "Enter")
    finally:
        subprocess.run(["tmux", "delete-buffer", "-b", buffer_name], capture_output=True, check=False)


def ask(entry: dict[str, str], question: str, *, timeout: int = 180,
        poll_interval: float = 1.0, force: bool = False) -> dict[str, str | float]:
    pane = entry["pane"]
    kind = entry["kind"]
    info = pane_info(pane)
    before = capture_pane(pane, 80)
    if not force and not pane_is_ready(kind, before):
        raise TmuxChatError(f"{pane} ({kind}) is busy or not at an input prompt; use --force only after inspection")

    token = secrets.token_hex(12)
    prefix = f"TMUX_BRIDGE_REPLY_{token}:"
    prompt = (
        "[TMUX_BRIDGE_REQUEST] 请直接回答这个问题，不要调用 hermes-mailbox："
        f"{question}。回答后在同一次回复的最后追加：{prefix} "
        "后接完整回答的合法 JSON 字符串；必须把标记和 JSON 放在同一行，不要用 Markdown 代码块。"
    )
    paste_and_submit(pane, prompt, submit_delay=0.25 if kind == "hermes" else 0.0)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        output = capture_pane(pane, 1000)
        reply = extract_reply(output, token)
        if reply is not None:
            return {
                "reply": reply,
                "pane": pane,
                "kind": kind,
                "target": info["target"],
                "token": token,
            }
    raise TmuxChatError(f"timed out after {timeout}s waiting for reply from {pane}")


def default_registry() -> Path:
    return Path(__file__).with_name("tmux-panes.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask already-running Claude, Codex, or Hermes tmux panes")
    parser.add_argument("--registry", default=str(default_registry()))
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="Bind an alias to an immutable tmux pane id")
    register.add_argument("alias")
    register.add_argument("pane")
    register.add_argument("kind", choices=sorted(KINDS))

    commands.add_parser("list", help="List registered panes")

    capture = commands.add_parser("capture", help="Inspect a registered pane")
    capture.add_argument("alias")
    capture.add_argument("--lines", type=int, default=40)

    ask_parser = commands.add_parser("ask", help="Ask a registered interactive pane and wait for its marked reply")
    ask_parser.add_argument("alias")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--timeout", type=int, default=180)
    ask_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()
    registry = Registry(args.registry)
    try:
        if args.command == "register":
            pane_info(args.pane)
            registry.register(args.alias, args.pane, args.kind)
            result: object = {"alias": args.alias, **registry.resolve(args.alias), "status": "registered"}
        elif args.command == "list":
            result = registry.list()
        elif args.command == "capture":
            entry = registry.resolve(args.alias)
            result = capture_pane(entry["pane"], args.lines)
            print(result, end="" if result.endswith("\n") else "\n")
            return
        else:
            result = ask(
                registry.resolve(args.alias), args.question,
                timeout=args.timeout, force=args.force,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TmuxChatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
