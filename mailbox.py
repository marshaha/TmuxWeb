#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mcp_server import Mailbox

DEFAULT_DB = Path(__file__).with_name("mailbox.db")


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local Claude/Codex/Hermes mailbox")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    send = subparsers.add_parser("send", help="Send a message as Hermes")
    send.add_argument("to", choices=["claude", "codex"])
    send.add_argument("body")
    send.add_argument("--topic")

    inbox = subparsers.add_parser("inbox", help="Read messages addressed to Hermes")
    inbox.add_argument("--all", action="store_true", help="Include previously read messages")
    inbox.add_argument("--keep-unread", action="store_true")
    inbox.add_argument("--limit", type=int, default=20)

    reply = subparsers.add_parser("reply", help="Reply as Hermes")
    reply.add_argument("message_id", type=int)
    reply.add_argument("body")

    args = parser.parse_args()
    mailbox = Mailbox(args.db)
    if args.command == "send":
        result = mailbox.send("hermes", args.to, args.body, args.topic)
    elif args.command == "inbox":
        result = mailbox.receive(
            "hermes", unread_only=not args.all, limit=args.limit, mark_read=not args.keep_unread
        )
    else:
        result = mailbox.reply(args.message_id, "hermes", args.body)
    print_json(result)


if __name__ == "__main__":
    main()
