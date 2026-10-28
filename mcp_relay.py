#!/usr/bin/env python3
"""TmuxWeb relay MCP server (stdio, no dependencies).

One install per agent CLI, one endpoint: every host TmuxWeb manages becomes
reachable from any machine. The agent gets four tools:

    list_agents    which agent panes exist, on which host, idle or busy
    ask_agent      ask one of them a question and wait for the reply
    send_agent     type into its pane without waiting
    read_agent     read its screen (or its structured transcript)

Environment:

    TMUXWEB_URL    http://127.0.0.1:8787 by default
    TMUXWEB_TOKEN  the server's TMUX_WEB_API_TOKEN (required)

Register it with the agent CLI, e.g.:

    codex mcp add tmuxweb --env TMUXWEB_URL=http://robo:8787 \
        --env TMUXWEB_TOKEN=... -- python3 /path/to/mcp_relay.py

    claude mcp add -s user tmuxweb --env ... -- python3 /path/to/mcp_relay.py

Check an install without an agent in the loop:

    TMUXWEB_URL=... TMUXWEB_TOKEN=... python3 mcp_relay.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from typing import Any, BinaryIO

from relay_client import RelayError, base_url, request, token

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "tmuxweb-relay"
DEFAULT_ASK_TIMEOUT = 240


def pane_path(target: str, suffix: str = "") -> str:
    return f"/api/panes/{urllib.parse.quote(str(target), safe='')}{suffix}"


class RelayTools:
    """The tools themselves, kept free of protocol details so they stay testable."""

    def list_agents(self, arguments: dict) -> dict:
        peers = request("/api/relay/peers")["peers"]
        if not arguments.get("include_non_agents", False):
            peers = [peer for peer in peers if peer.get("provider")]
        return {"agents": [
            {
                "target": peer["id"],
                "host": peer.get("host_name") or peer.get("host_id"),
                "session": (peer.get("target") or "").split(":")[0],
                "window": peer.get("target"),
                "provider": peer.get("provider"),
                "status": peer.get("status"),
                "path": peer.get("path"),
                "title": peer.get("title"),
            }
            for peer in peers
        ]}

    def ask_agent(self, arguments: dict) -> dict:
        target = self._target(arguments)
        question = self._text(arguments, "question", limit=20_000)
        timeout = int(arguments.get("timeout") or DEFAULT_ASK_TIMEOUT)
        timeout = max(5, min(timeout, 600))
        result = request("/api/relay/ask", {
            "target": target,
            "question": question,
            "timeout": timeout,
            "force": bool(arguments.get("force", False)),
            "source": arguments.get("source") or "mcp",
        }, timeout=timeout + 30)
        return {"target": target, "reply": result.get("reply", ""), "pane": result.get("pane")}

    def send_agent(self, arguments: dict) -> dict:
        target = self._target(arguments)
        text = self._text(arguments, "text", limit=20_000)
        result = request("/api/relay/message", {"target": target, "text": text,
                                                "submit": bool(arguments.get("submit", True))})
        return {"target": target, "sent": True, "pane": result.get("pane")}

    def read_agent(self, arguments: dict) -> dict:
        target = self._target(arguments)
        if arguments.get("transcript"):
            limit = max(1, min(int(arguments.get("limit") or 50), 300))
            result = request(pane_path(target, f"/transcript?limit={limit}"))
            return {"target": target, "provider": result.get("provider"),
                    "session": result.get("session_id"), "messages": result.get("messages", [])}
        history = max(10, min(int(arguments.get("lines") or 80), 5000))
        result = request(pane_path(target, f"/capture?history={history}"))
        return {"target": target, "content": result.get("content", "")}

    def publish_home_card(self, arguments: dict) -> dict:
        """Put a card on the Home board so a human can see it at /home."""
        title = self._text(arguments, "title", limit=200)
        body = str(arguments.get("body") or "")[:20_000]
        pane = str(arguments.get("pane") or "").strip()
        kind = str(arguments.get("kind") or "note").strip() or "note"
        result = request("/api/home/cards", {"title": title, "body": body, "pane": pane, "kind": kind})
        return {"card": result, "visible_at": "/home"}

    def list_remote_hosts(self, arguments: dict) -> dict:
        hosts = request("/api/remote-hosts").get("hosts", [])
        return {"hosts": [
            {"id": host.get("id"), "name": host.get("name"), "hostname": host.get("hostname"),
             "port": host.get("port"), "username": host.get("username"),
             "auth_type": host.get("auth_type"), "jump_host_id": host.get("jump_host_id") or "",
             "jump_spec": host.get("jump_spec") or "",
             "fingerprint": host.get("fingerprint") or ""}
            for host in hosts
        ]}

    def add_remote_host(self, arguments: dict) -> dict:
        """Register an SSH host (optionally behind a jump host)."""
        payload = {
            "name": self._text(arguments, "name", limit=64),
            "hostname": self._text(arguments, "hostname", limit=255),
            "username": self._text(arguments, "username", limit=64),
            "port": int(arguments.get("port") or 22),
            "auth_type": str(arguments.get("auth_type") or "agent").strip() or "agent",
            "credential": str(arguments.get("credential") or ""),
            "jump_host_id": str(arguments.get("jump_host") or "").strip(),
            "jump_spec": str(arguments.get("jump") or "").strip(),
            "verify": bool(arguments.get("verify", True)),
        }
        result = request("/api/remote-hosts", payload, timeout=90)
        return {"host": {key: result.get(key) for key in
                         ("id", "name", "hostname", "port", "username", "auth_type", "jump_host_id",
                          "jump_spec")},
                "note": "the console sidebar picks it up on its next refresh"}

    def remove_remote_host(self, arguments: dict) -> dict:
        host_id = self._text(arguments, "id", limit=64)
        request(f"/api/remote-hosts/{host_id}", method="DELETE")
        return {"id": host_id, "removed": True}

    def list_home(self, arguments: dict) -> dict:
        limit = max(1, min(int(arguments.get("limit") or 20), 60))
        board = request("/api/home")
        board["cards"] = board.get("cards", [])[:limit]
        board["favorites"] = board.get("favorites", [])[:limit]
        return board

    def remove_home_card(self, arguments: dict) -> dict:
        card_id = self._text(arguments, "id", limit=64)
        request(f"/api/home/cards/{card_id}", method="DELETE")
        return {"id": card_id, "removed": True}

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _target(arguments: dict) -> str:
        value = str(arguments.get("target") or "").strip()
        if not value:
            raise ValueError("target is required, e.g. \"%12\" or \"<host_id>~%3\" (see list_agents)")
        return value

    @staticmethod
    def _text(arguments: dict, field: str, *, limit: int) -> str:
        value = str(arguments.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field} must not be empty")
        if len(value) > limit:
            raise ValueError(f"{field} is longer than {limit} characters")
        return value

    def dispatch(self, name: str, arguments: dict) -> Any:
        handlers = {
            "list_agents": self.list_agents,
            "ask_agent": self.ask_agent,
            "send_agent": self.send_agent,
            "read_agent": self.read_agent,
            "publish_home_card": self.publish_home_card,
            "list_remote_hosts": self.list_remote_hosts,
            "add_remote_host": self.add_remote_host,
            "remove_remote_host": self.remove_remote_host,
            "list_home": self.list_home,
            "remove_home_card": self.remove_home_card,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        return handler(arguments or {})


def tool_schemas() -> list[dict]:
    """The tool catalogue, shared with the server-side /mcp endpoint."""
    target = {"type": "string", "description": "pane target from list_agents, e.g. \"%12\" or \"<host_id>~%3\""}
    return [
        {
            "name": "list_agents",
            "description": ("List agent panes across every host this TmuxWeb server manages, with a target you can pass "
                            "to the other tools, the host it lives on, the CLI running in it and whether it is idle or busy."),
            "inputSchema": {
                "type": "object",
                "properties": {"include_non_agents": {"type": "boolean", "default": False}},
                "additionalProperties": False,
            },
        },
        {
            "name": "ask_agent",
            "description": ("Ask another agent (on this or any other host) a question and return its answer. "
                            "Fails fast when the target is busy; use read_agent to inspect it first."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": target,
                    "question": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "timeout": {"type": "integer", "minimum": 5, "maximum": 600, "default": DEFAULT_ASK_TIMEOUT},
                    "force": {"type": "boolean", "default": False,
                              "description": "send even when the pane does not look idle"},
                },
                "required": ["target", "question"],
                "additionalProperties": False,
            },
        },
        {
            "name": "send_agent",
            "description": "Type a message into another agent's pane without waiting for an answer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": target,
                    "text": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "submit": {"type": "boolean", "default": True},
                },
                "required": ["target", "text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_remote_hosts",
            "description": ("List the SSH hosts this TmuxWeb server knows: id, name, address, auth type and the jump host it "
                            "dials through. Credentials are never returned."),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "add_remote_host",
            "description": ("Register an SSH host so its tmux panes show up in the console, optionally through a jump host. "
                            "Defaults to auth_type=agent (your local ssh agent key), so no secret needs to be passed. "
                            "verify=true fails when the host cannot be reached, verify=false stores it anyway."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "hostname": {"type": "string", "minLength": 1, "maxLength": 255},
                    "username": {"type": "string", "minLength": 1, "maxLength": 64},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 22},
                    "auth_type": {"type": "string", "enum": ["agent", "password", "key"], "default": "agent"},
                    "credential": {"type": "string", "maxLength": 100000,
                                   "description": "password or private key text; omit for auth_type=agent"},
                    "jump_host": {"type": "string",
                                  "description": "id of another saved host to dial through (see list_remote_hosts)"},
                    "jump": {"type": "string",
                             "description": "or type a jump host as user@host:port; it reuses this host's credential"},
                    "verify": {"type": "boolean", "default": True},
                },
                "required": ["name", "hostname", "username"],
                "additionalProperties": False,
            },
        },
        {
            "name": "remove_remote_host",
            "description": "Forget a saved SSH host by id; hosts that jumped through it fall back to a direct connection.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 64}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "publish_home_card",
            "description": ("Publish a card to the TmuxWeb Home board (/home) so the human sees your result without opening this pane. "
                            "Use it for a finished summary, a decision that needs attention, or a link worth keeping."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "body": {"type": "string", "maxLength": 20000,
                             "description": "plain text or markdown; keep it short enough to read on a card"},
                    "pane": {"type": "string", "description": "optional pane target this card belongs to, e.g. \"%12\""},
                    "kind": {"type": "string", "enum": ["note", "result", "alert", "link"], "default": "note"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_home",
            "description": "Read the Home board: favourited panes with their status and last message, plus published cards.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20}},
                "additionalProperties": False,
            },
        },
        {
            "name": "remove_home_card",
            "description": "Remove a card you published from the Home board by its id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 64}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_agent",
            "description": ("Read another agent's pane: plain screen text by default, or its structured transcript "
                            "(messages with roles) when transcript=true."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": target,
                    "lines": {"type": "integer", "minimum": 10, "maximum": 5000, "default": 80},
                    "transcript": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 300, "default": 50},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        },
    ]


_TOOL_SCHEMAS: list[dict] = tool_schemas()



class MCPServer:
    def __init__(self, tools: RelayTools):
        self.tools = tools

    def _tools(self) -> list[dict]:
        return tool_schemas()

    @staticmethod
    def _result(payload: object, *, is_error: bool = False) -> dict:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    def handle(self, request_payload: dict) -> dict | None:
        request_id = request_payload.get("id")
        method = request_payload.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                params = request_payload.get("params") or {}
                payload = self.tools.dispatch(str(params.get("name") or ""), params.get("arguments") or {})
                result = self._result(payload)
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": self._result({"error": str(exc)}, is_error=True)}
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
        except RelayError as exc:
            detail: dict[str, Any] = {"error": str(exc)}
            if getattr(exc, "tail", ""):
                detail["pane_tail"] = exc.tail
            return {"jsonrpc": "2.0", "id": request_id, "result": self._result(detail, is_error=True)}

    def serve(self, source: BinaryIO, sink: BinaryIO) -> None:
        for raw_line in source:
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
                response = self.handle(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            if response is not None:
                sink.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                sink.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TmuxWeb relay MCP server (stdio)")
    parser.add_argument("--selftest", action="store_true",
                        help="print the server URL and the agents it can see, then exit")
    args = parser.parse_args(argv)
    if args.selftest:
        print(f"server: {base_url()}  token: {'set' if token() else 'missing'}")
        try:
            agents = RelayTools().list_agents({})["agents"]
        except RelayError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for agent in agents:
            print(f"  {agent['target']:<24} {agent['status']:<8} {str(agent['provider'] or '-'):<8} "
                  f"{agent['host']:<16} {agent['title']}")
        print(f"{len(agents)} agent pane(s)")
        return 0
    token()  # fail loudly at startup instead of on the first tool call
    MCPServer(RelayTools()).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
