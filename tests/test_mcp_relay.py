import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_relay
from relay_client import RelayError

PEERS = [
    {"id": "%7", "pane_id": "%7", "host_id": "localhost", "host_name": "localhost",
     "target": "codex:0.0", "provider": "codex", "status": "idle",
     "title": "review", "path": "/work/repo"},
    {"id": "abc~%3", "pane_id": "%3", "host_id": "abc", "host_name": "build01",
     "target": "claude:1.0", "provider": "claude", "status": "busy",
     "title": "agent", "path": "/srv/app"},
    {"id": "%9", "pane_id": "%9", "host_id": "localhost", "host_name": "localhost",
     "target": "notes:0.0", "provider": None, "status": "idle", "title": "shell", "path": "/tmp"},
]


class Recorder:
    """Stands in for relay_client.request and records what the tools asked for."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, path, payload=None, *, timeout=30.0):
        self.calls.append((path, payload, timeout))
        for key, value in self.responses.items():
            if key in path:
                return value
        return {}


class ToolTests(unittest.TestCase):
    def test_list_agents_hides_non_agent_panes_by_default(self):
        recorder = Recorder({"/api/relay/peers": {"peers": PEERS}})
        with patch.object(mcp_relay, "request", recorder):
            agents = mcp_relay.RelayTools().list_agents({})["agents"]
            everything = mcp_relay.RelayTools().list_agents({"include_non_agents": True})["agents"]
        self.assertEqual([agent["target"] for agent in agents], ["%7", "abc~%3"])
        self.assertEqual(len(everything), 3)
        self.assertEqual(agents[1]["host"], "build01")
        self.assertEqual(agents[1]["session"], "claude")
        self.assertEqual(agents[1]["status"], "busy")

    def test_ask_agent_sends_the_question_and_clamps_the_timeout(self):
        recorder = Recorder({"/api/relay/ask": {"reply": "42", "pane": "%7"}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().ask_agent({"target": "%7", "question": "  the answer?  ",
                                                       "timeout": 5000})
        path, payload, timeout = recorder.calls[0]
        self.assertEqual(path, "/api/relay/ask")
        self.assertEqual(payload["question"], "the answer?")
        self.assertEqual(payload["timeout"], 600)
        self.assertEqual(timeout, 630)
        self.assertEqual(result["reply"], "42")

    def test_send_agent_posts_a_message(self):
        recorder = Recorder({"/api/relay/message": {"pane": "abc~%3"}})
        with patch.object(mcp_relay, "request", recorder):
            mcp_relay.RelayTools().send_agent({"target": "abc~%3", "text": "run the tests"})
        path, payload, _ = recorder.calls[0]
        self.assertEqual(path, "/api/relay/message")
        self.assertEqual(payload, {"target": "abc~%3", "text": "run the tests", "submit": True})

    def test_read_agent_uses_capture_or_transcript(self):
        recorder = Recorder({"/capture": {"content": "screen text"},
                             "/transcript": {"provider": "codex", "session_id": "s1",
                                             "messages": [{"id": "1", "role": "user", "text": "hi"}]}})
        with patch.object(mcp_relay, "request", recorder):
            tools = mcp_relay.RelayTools()
            captured = tools.read_agent({"target": "abc~%3", "lines": 40})
            transcript = tools.read_agent({"target": "abc~%3", "transcript": True, "limit": 5})
        self.assertEqual(captured["content"], "screen text")
        # the host~pane separator has to survive URL encoding
        self.assertEqual(recorder.calls[0][0], "/api/panes/abc~%253/capture?history=40")
        self.assertEqual(recorder.calls[1][0], "/api/panes/abc~%253/transcript?limit=5")
        self.assertEqual(transcript["messages"][0]["text"], "hi")

    def test_arguments_are_validated(self):
        tools = mcp_relay.RelayTools()
        for arguments in ({}, {"target": "  "}):
            with self.assertRaises(ValueError):
                tools.read_agent(arguments)
        with self.assertRaises(ValueError):
            tools.ask_agent({"target": "%7", "question": "   "})
        with self.assertRaises(ValueError):
            tools.ask_agent({"target": "%7", "question": "x" * 20001})
        with self.assertRaises(ValueError):
            tools.dispatch("nope", {})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = mcp_relay.MCPServer(mcp_relay.RelayTools())

    def test_initialize_and_tools_list(self):
        init = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["protocolVersion"], mcp_relay.PROTOCOL_VERSION)
        self.assertEqual(init["result"]["serverInfo"]["name"], "tmuxweb-relay")
        listing = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
        self.assertEqual(set(tools), {"list_agents", "ask_agent", "send_agent", "read_agent"})
        self.assertEqual(tools["ask_agent"]["inputSchema"]["required"], ["target", "question"])

    def test_notifications_are_ignored_and_unknown_methods_rejected(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        unknown = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_tool_calls_return_json_text(self):
        recorder = Recorder({"/api/relay/peers": {"peers": PEERS}})
        with patch.object(mcp_relay, "request", recorder):
            response = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                           "params": {"name": "list_agents", "arguments": {}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(len(payload["agents"]), 2)
        self.assertFalse(response["result"]["isError"])

    def test_busy_pane_errors_keep_the_pane_tail(self):
        def failing(path, payload=None, *, timeout=30.0):
            raise RelayError("pane looks busy", status=409, tail="esc to interrupt")

        with patch.object(mcp_relay, "request", failing):
            response = self.server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                           "params": {"name": "ask_agent",
                                                      "arguments": {"target": "%7", "question": "hi"}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error"], "pane looks busy")
        self.assertEqual(payload["pane_tail"], "esc to interrupt")

    def test_invalid_arguments_become_tool_errors(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                       "params": {"name": "ask_agent", "arguments": {"target": "%7"}}})
        self.assertTrue(response["result"]["isError"])

    def test_serve_reads_line_delimited_json(self):
        recorder = Recorder({"/api/relay/peers": {"peers": PEERS}})
        source = io.BytesIO(
            (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
             + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "list_agents", "arguments": {}}}) + "\n").encode())
        sink = io.BytesIO()
        with patch.object(mcp_relay, "request", recorder):
            self.server.serve(source, sink)
        lines = [json.loads(line) for line in sink.getvalue().decode().splitlines()]
        self.assertEqual([line["id"] for line in lines], [1, 2])
        self.assertIn("agents", lines[1]["result"]["content"][0]["text"])


class InstallerTests(unittest.TestCase):
    def test_installer_never_prints_the_token(self):
        script = Path("install_relay_mcp.sh").read_text()
        self.assertIn("redact()", script)
        self.assertIn('s/$TOKEN/***REDACTED***/g', script)
        self.assertIn("chmod 600", script)
        # hermes has no --yes flag, so the enable prompt is answered explicitly
        self.assertIn("printf 'y\\n' | hermes mcp add", script)


class MissingTokenTests(unittest.TestCase):
    def test_token_falls_back_to_the_server_variable(self):
        with patch.dict(os.environ, {"TMUXWEB_TOKEN": "", "TMUX_WEB_API_TOKEN": "", "TMUXWEB_URL": ""}, clear=False):
            from relay_client import base_url, token
            with self.assertRaises(RelayError):
                token()
            self.assertEqual(base_url(), "http://127.0.0.1:8787")
        with patch.dict(os.environ, {"TMUX_WEB_API_TOKEN": "abc"}, clear=False):
            from relay_client import token
            self.assertEqual(token(), "abc")


if __name__ == "__main__":
    unittest.main()
