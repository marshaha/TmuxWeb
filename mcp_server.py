#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

ALLOWED_IDENTITIES = {"hermes", "claude", "codex"}
PROTOCOL_VERSION = "2025-06-18"


class Mailbox:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    body TEXT NOT NULL,
                    topic TEXT,
                    reply_to INTEGER REFERENCES messages(id),
                    created_at TEXT NOT NULL,
                    read_at TEXT
                )
                """
            )

    @staticmethod
    def _identity(value: str, field: str) -> str:
        if value not in ALLOWED_IDENTITIES:
            raise ValueError(f"{field} must be one of: {', '.join(sorted(ALLOWED_IDENTITIES))}")
        return value

    @staticmethod
    def _body(value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("body must not be empty")
        if len(value) > 100_000:
            raise ValueError("body is too long")
        return value

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return dict(row)

    def send(self, sender: str, recipient: str, body: str, topic: str | None = None,
             reply_to: int | None = None) -> dict:
        sender = self._identity(sender, "sender")
        recipient = self._identity(recipient, "recipient")
        body = self._body(body)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(sender, recipient, body, topic, reply_to, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sender, recipient, body, topic, reply_to, now),
            )
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = self._row(row)
        result["status"] = "pending"
        return result

    def receive(self, recipient: str, *, unread_only: bool = True, limit: int = 20,
                mark_read: bool = False) -> list[dict]:
        recipient = self._identity(recipient, "recipient")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        condition = "AND read_at IS NULL" if unread_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM messages WHERE recipient = ? {condition} ORDER BY id ASC LIMIT ?",
                (recipient, limit),
            ).fetchall()
            if mark_read and rows:
                now = datetime.now(timezone.utc).isoformat()
                connection.executemany(
                    "UPDATE messages SET read_at = COALESCE(read_at, ?) WHERE id = ?",
                    [(now, row["id"]) for row in rows],
                )
                rows = connection.execute(
                    f"SELECT * FROM messages WHERE id IN ({','.join('?' for _ in rows)}) ORDER BY id ASC",
                    [row["id"] for row in rows],
                ).fetchall()
        return [self._row(row) for row in rows]

    def reply(self, message_id: int, sender: str, body: str) -> dict:
        sender = self._identity(sender, "sender")
        with self._connect() as connection:
            original = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if original is None:
            raise ValueError("message not found")
        if original["recipient"] != sender:
            raise ValueError("message was not addressed to this identity")
        return self.send(sender, original["sender"], body, original["topic"], message_id)


class MCPServer:
    def __init__(self, mailbox: Mailbox, identity: str):
        self.mailbox = mailbox
        self.identity = Mailbox._identity(identity, "identity")

    def _tools(self) -> list[dict]:
        peers = sorted(ALLOWED_IDENTITIES - {self.identity})
        return [
            {
                "name": "send_message",
                "description": f"Send a text message as {self.identity}. Allowed recipients: {', '.join(peers)}.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "enum": peers},
                        "body": {"type": "string", "minLength": 1, "maxLength": 100000},
                        "topic": {"type": "string"},
                    },
                    "required": ["to", "body"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "receive_messages",
                "description": f"Read messages addressed to {self.identity}. No files, shell, or network access is exposed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "unread_only": {"type": "boolean", "default": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                        "mark_read": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "reply_message",
                "description": f"Reply as {self.identity} to a message that was addressed to {self.identity}.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "integer", "minimum": 1},
                        "body": {"type": "string", "minLength": 1, "maxLength": 100000},
                    },
                    "required": ["message_id", "body"],
                    "additionalProperties": False,
                },
            },
        ]

    @staticmethod
    def _result(payload: object, *, is_error: bool = False) -> dict:
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        }

    def handle(self, request: dict) -> dict | None:
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "local-agent-mailbox", "version": "1.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name == "send_message":
                    payload = self.mailbox.send(
                        self.identity, arguments["to"], arguments["body"], arguments.get("topic")
                    )
                elif name == "receive_messages":
                    payload = self.mailbox.receive(
                        self.identity,
                        unread_only=arguments.get("unread_only", True),
                        limit=arguments.get("limit", 20),
                        mark_read=arguments.get("mark_read", True),
                    )
                elif name == "reply_message":
                    payload = self.mailbox.reply(arguments["message_id"], self.identity, arguments["body"])
                else:
                    raise ValueError(f"unknown tool: {name}")
                result = self._result(payload)
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request_id, "result": self._result({"error": str(exc)}, is_error=True)}
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}

    def serve(self, source: BinaryIO, sink: BinaryIO) -> None:
        for raw_line in source:
            if not raw_line.strip():
                continue
            try:
                request = json.loads(raw_line)
                response = self.handle(request)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            if response is not None:
                sink.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                sink.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled local mailbox MCP server")
    parser.add_argument("--identity", required=True, choices=sorted(ALLOWED_IDENTITIES))
    parser.add_argument("--db", default=str(Path(__file__).with_name("mailbox.db")))
    args = parser.parse_args()
    MCPServer(Mailbox(args.db), args.identity).serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
