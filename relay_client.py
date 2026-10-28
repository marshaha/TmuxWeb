#!/usr/bin/env python3
"""TmuxWeb relay client.

Talks to a TmuxWeb server over HTTP with an API token, so an agent (or a shell)
on one server can drive agent panes on any host that server manages.

    export TMUXWEB_URL=https://tmux.example.internal
    export TMUXWEB_TOKEN=...
    ./relay_client.py peers
    ./relay_client.py ask '%12' '总结一下 hermes 会话里的部署结论' --timeout 240
    ./relay_client.py send 'build01~%3' '请先跑一遍单元测试'

The pane reference is either ``%pane`` (localhost) or ``<host_id>~%pane``; use
``peers`` to discover both.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class RelayError(RuntimeError):
    """A failed call, carrying the HTTP status and any pane tail the server sent."""

    def __init__(self, message: str, status: int = 0, tail: str = ""):
        super().__init__(message)
        self.status = status
        self.tail = tail


def base_url() -> str:
    return (os.environ.get("TMUXWEB_URL") or "http://127.0.0.1:8787").rstrip("/")


def token() -> str:
    value = os.environ.get("TMUXWEB_TOKEN") or os.environ.get("TMUX_WEB_API_TOKEN") or ""
    if not value:
        raise RelayError("set TMUXWEB_TOKEN to the server's TMUX_WEB_API_TOKEN")
    return value


def request(path: str, payload: dict | None = None, *, timeout: float = 30.0,
            method: str | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    verb = method or ("POST" if data is not None else "GET")
    request = urllib.request.Request(base_url() + path, data=data, method=verb)
    request.add_header("Authorization", f"Bearer {token()}")
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"detail": body.strip()}
        message = detail.get("detail") if isinstance(detail, dict) else body
        tail = detail.get("tail", "") if isinstance(detail, dict) else ""
        raise RelayError(f"{message}", status=exc.code, tail=tail)
    except urllib.error.URLError as exc:
        raise RelayError(f"cannot reach {base_url()}: {exc.reason}")
    return json.loads(body or "{}")


def call(path: str, payload: dict | None = None, *, timeout: float = 30.0) -> Any:
    """Backwards-compatible alias for ``request``."""
    return request(path, payload, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-server relay for TmuxWeb agent panes")
    parser.add_argument("--url", default=None, help="server URL (default: $TMUXWEB_URL)")
    parser.add_argument("--token", default=None, help="API token (default: $TMUXWEB_TOKEN)")
    commands = parser.add_subparsers(dest="command", required=True)

    peers = commands.add_parser("peers", help="list agent panes on every managed host")
    peers.add_argument("--all", action="store_true", help="include panes that are not agent CLIs")

    ask = commands.add_parser("ask", help="ask one agent pane and wait for its answer")
    ask.add_argument("target")
    ask.add_argument("question")
    ask.add_argument("--timeout", type=int, default=180)
    ask.add_argument("--force", action="store_true", help="send even if the pane looks busy")
    ask.add_argument("--source", default="", help="label shown to the target agent")

    send = commands.add_parser("send", help="send a message without waiting")
    send.add_argument("target")
    send.add_argument("text")

    capture = commands.add_parser("capture", help="print the current pane text")
    capture.add_argument("target")
    capture.add_argument("--lines", type=int, default=80)

    transcript = commands.add_parser("transcript", help="print the structured transcript as JSON")
    transcript.add_argument("target")
    transcript.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    if args.url:
        os.environ["TMUXWEB_URL"] = args.url
    if args.token:
        os.environ["TMUXWEB_TOKEN"] = args.token

    try:
        return _run_command(args)
    except RelayError as exc:
        detail = f" ({exc.status})" if exc.status else ""
        raise SystemExit(f"error{detail}: {exc}") from None


def _run_command(args) -> int:
    if args.command == "peers":
        peers_data = call("/api/relay/peers")["peers"]
        if not args.all:
            peers_data = [item for item in peers_data if item["provider"]]
        if not peers_data:
            print("no agent panes found", file=sys.stderr)
        for item in peers_data:
            print(f"{item['id']:<24} {item['status']:<8} {str(item['provider'] or '-'):<8} "
                  f"{item['host_name']:<16} {item['target']:<24} {item['title']}")
        return 0
    if args.command == "ask":
        result = call("/api/relay/ask", {"target": args.target, "question": args.question,
                                         "timeout": args.timeout, "force": args.force,
                                         "source": args.source or "relay_cli"},
                      timeout=args.timeout + 30)
        print(result["reply"])
        return 0
    if args.command == "send":
        print(json.dumps(call("/api/relay/message", {"target": args.target, "text": args.text}),
                         ensure_ascii=False))
        return 0
    if args.command == "capture":
        result = call(f"/api/panes/{urllib.parse.quote(args.target, safe='')}/capture?history={args.lines}")
        print(result["content"], end="" if result["content"].endswith("\n") else "\n")
        return 0
    result = call(f"/api/panes/{urllib.parse.quote(args.target, safe='')}/transcript?limit={args.limit}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
