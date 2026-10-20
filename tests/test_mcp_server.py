import io
import json
import tempfile
import unittest
from pathlib import Path

from mcp_server import Mailbox, MCPServer


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mailbox = Mailbox(Path(self.tmp.name) / "mailbox.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_send_receive_and_reply(self):
        sent = self.mailbox.send("hermes", "claude", "请检查设计", "design")
        self.assertEqual(sent["status"], "pending")

        inbox = self.mailbox.receive("claude")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["body"], "请检查设计")

        reply = self.mailbox.reply(sent["id"], "claude", "设计可行")
        self.assertEqual(reply["reply_to"], sent["id"])
        hermes_inbox = self.mailbox.receive("hermes")
        self.assertEqual(hermes_inbox[0]["body"], "设计可行")

    def test_receive_can_filter_unread_and_mark_read(self):
        self.mailbox.send("hermes", "codex", "one")
        first = self.mailbox.receive("codex", mark_read=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(self.mailbox.receive("codex", unread_only=True), [])

    def test_reply_rejects_wrong_recipient(self):
        sent = self.mailbox.send("hermes", "claude", "secret")
        with self.assertRaisesRegex(ValueError, "not addressed"):
            self.mailbox.reply(sent["id"], "codex", "nope")


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mailbox = Mailbox(Path(self.tmp.name) / "mailbox.db")
        self.server = MCPServer(mailbox, identity="claude")

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_and_list_tools(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")

        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], [
            "send_message", "receive_messages", "reply_message"
        ])

    def test_tool_call_uses_fixed_identity(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "send_message", "arguments": {"to": "hermes", "body": "hello"}},
        })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["sender"], "claude")
        self.assertEqual(payload["recipient"], "hermes")

    def test_stdio_uses_newline_delimited_json(self):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
        source = io.BytesIO(request)
        sink = io.BytesIO()
        self.server.serve(source, sink)
        output = sink.getvalue()
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn(b'"result":{}', output)


if __name__ == "__main__":
    unittest.main()
