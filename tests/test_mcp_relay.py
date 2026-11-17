import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
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

    def __call__(self, path, payload=None, *, timeout=30.0, method=None):
        self.calls.append((path, payload, timeout))
        if method:
            self.methods = getattr(self, "methods", [])
            self.methods.append(method)
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

    def test_create_session_passes_the_agent_and_returns_the_pane(self):
        recorder = Recorder({"/api/hosts": {"hosts": [{"id": "abc123", "name": "oracle"}]},
                             "/api/sessions": {"host": "abc123",
                                               "session": {"session": "aiisp", "path": "/srv/aiisp",
                                                           "created_dir": True, "command": "claude"},
                                               "panes": [{"id": "abc123~%156", "handle": "#153"}]},
                             "/api/relay/peers": {"peers": [{"id": "abc123~%156", "number": 153}]}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().create_session({
                "name": "aiisp", "host": "oracle", "path": "/srv/aiisp",
                "agent": "claude", "agent_args": "--model sonnet"})
        path, payload, _ = [call for call in recorder.calls if call[0] == "/api/sessions"][0]
        self.assertEqual(path, "/api/sessions")
        self.assertEqual(payload["host"], "abc123", "主机名要解析成 id")
        self.assertEqual(payload["agent"], "claude")
        self.assertEqual(payload["agent_args"], "--model sonnet")
        self.assertTrue(payload["keep_open"])
        self.assertEqual(result["session"], "aiisp")
        self.assertEqual(result["handle"], "#153")
        self.assertTrue(result["created_dir"])

    def test_create_session_without_agent_stays_a_plain_shell(self):
        recorder = Recorder({"/api/sessions": {"host": "localhost",
                                               "session": {"session": "dev", "path": ""}}})
        with patch.object(mcp_relay, "request", recorder):
            mcp_relay.RelayTools().create_session({"name": "dev"})
        payload = [call for call in recorder.calls if call[0] == "/api/sessions"][0][1]
        self.assertNotIn("agent", payload)
        self.assertEqual(payload["host"], "localhost")

    def test_kill_session_can_aim_at_one_pane(self):
        recorder = Recorder({"/api/panes/abc~%253": {"pane": "abc~%3", "session": "work",
                                                     "session_alive": True}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().kill_session({"pane": "abc~%3"})
        self.assertEqual(recorder.calls[0][0], "/api/panes/abc~%253")
        self.assertEqual(recorder.methods, ["DELETE"])
        self.assertEqual(result["scope"], "pane")
        self.assertTrue(result["session_alive"])

    def test_kill_session_resolves_the_host_name(self):
        recorder = Recorder({"/api/hosts": {"hosts": [{"id": "abc123", "name": "Oracle"}]},
                             "/api/sessions/aiisp": {"session": "aiisp", "host": "abc123"}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().kill_session({"session": "aiisp", "host": "oracle"})
        paths = [call[0] for call in recorder.calls]
        self.assertIn("/api/sessions/aiisp?host=abc123", paths)
        self.assertTrue(result["killed"])

    def test_send_file_uploads_into_the_target_upload_folder(self):
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/files/upload": {"status": "uploaded", "path": "/srv/work/upload_files/plan.md",
                              "size": 5},
            "/api/relay/message": {"pane": "abc~%3"}})
        raw_calls = []

        def raw(path, data, **kwargs):
            raw_calls.append((path, data))
            return {"status": "uploaded", "path": "/srv/work/upload_files/plan.md", "size": len(data)}

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plan.md"
            source.write_bytes(b"hello")
            with patch.object(mcp_relay, "request", recorder), \
                    patch.object(mcp_relay, "request_raw", raw):
                result = mcp_relay.RelayTools().send_file({"target": "abc~%3", "path": str(source)})

        path, data = raw_calls[0]
        self.assertEqual(data, b"hello")
        self.assertIn("name=plan.md", path)
        self.assertIn("dir=%2Fsrv%2Fwork%2Fupload_files", path)
        self.assertEqual(result["path"], "/srv/work/upload_files/plan.md")
        self.assertTrue(result["notified"])
        # 目标 agent 得知道文件在哪
        message = [call for call in recorder.calls if call[0] == "/api/relay/message"][0][1]
        self.assertIn("/srv/work/upload_files/plan.md", message["text"])

    def test_send_file_chunks_a_large_file_and_resumes_on_409(self):
        """2.5MB 文件按 1MB 切块；第二块丢了（409 + 服务端已有字节数）就从对面
        的位置接着传，而不是从头再来；整份文件从不一次性读进内存。"""
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "abc~%3"}})
        calls = []
        plan = [
            {"status": "partial", "offset": 1024 * 1024, "size": 2621440},
            RelayError("upload offset mismatch", status=409, offset=1024 * 1024),
            {"status": "partial", "offset": 2 * 1024 * 1024, "size": 2621440},
            {"status": "uploaded", "path": "/srv/work/upload_files/big.bin",
             "size": 2621440},
        ]

        def raw(path, data, **kwargs):
            calls.append((path, data, kwargs.get("timeout")))
            step = plan[len(calls) - 1]
            if isinstance(step, Exception):
                raise step
            return step

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "big.bin"
            source.write_bytes(b"x" * 2621440)
            with patch.object(mcp_relay, "request", recorder), \
                    patch.object(mcp_relay, "request_raw", raw):
                result = mcp_relay.RelayTools().send_file({"target": "abc~%3", "path": str(source)})
        self.assertEqual([len(data) for _path, data, _t in calls],
                         [1024 * 1024, 1024 * 1024, 1024 * 1024, 512 * 1024])
        offsets = [urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["offset"][0]
                   for path, _data, _t in calls]
        self.assertEqual(offsets, ["0", "1048576", "1048576", "2097152"])
        tokens = {urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["token"][0]
                  for path, _data, _t in calls}
        self.assertEqual(len(tokens), 1)   # 同一份文件重试用同一个 .part
        self.assertTrue(all(timeout == 90.0 for _path, _data, timeout in calls))
        self.assertEqual(result["path"], "/srv/work/upload_files/big.bin")
        self.assertTrue(result["notified"])

    def test_send_file_halves_the_chunk_after_a_stalled_chunk(self):
        """一块在 90s 内没发完：块减半再试，而不是整个文件重来。"""
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "abc~%3"}})
        calls = []
        plan = [
            TimeoutError("timed out"),
            {"status": "partial", "offset": 512 * 1024, "size": 1572864},
            {"status": "partial", "offset": 1024 * 1024, "size": 1572864},
            {"status": "uploaded", "path": "/srv/work/upload_files/mid.bin",
             "size": 1572864},
        ]

        def raw(path, data, **kwargs):
            calls.append((path, data))
            step = plan[len(calls) - 1]
            if isinstance(step, Exception):
                raise step
            return step

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mid.bin"
            source.write_bytes(b"y" * 1572864)
            with patch.object(mcp_relay, "request", recorder), \
                    patch.object(mcp_relay, "request_raw", raw), \
                    patch.object(mcp_relay.time, "sleep"):
                result = mcp_relay.RelayTools().send_file({"target": "abc~%3", "path": str(source)})
        self.assertEqual([len(data) for _path, data in calls],
                         [1024 * 1024, 512 * 1024, 512 * 1024, 512 * 1024])
        self.assertEqual(result["size"], 1572864)

    def test_send_file_rejects_bad_input(self):
        tools = mcp_relay.RelayTools()
        with self.assertRaises(ValueError):
            tools.send_file({"target": "abc~%3"})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "a.txt"
            source.write_bytes(b"x")
            with self.assertRaises(ValueError):
                tools.send_file({"target": "abc~%3", "path": str(source), "name": "../escape.txt"})
            with self.assertRaises(ValueError):
                tools.send_file({"target": "abc~%3", "path": str(Path(directory) / "missing")})

    def test_send_file_accepts_base64_content(self):
        """content（base64）和 path 是同一条上传：字节一致、有 [file] 通知。"""
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "abc~%3"}})
        raw_calls = []

        def raw(path, data, **kwargs):
            raw_calls.append((path, data))
            return {"status": "uploaded", "path": "/srv/work/upload_files/plan.md",
                    "size": len(data)}

        with patch.object(mcp_relay, "request", recorder), \
                patch.object(mcp_relay, "request_raw", raw):
            result = mcp_relay.RelayTools().send_file({
                "target": "abc~%3", "name": "plan.md",
                "content": base64.b64encode(b"hello").decode()})
        self.assertEqual(raw_calls[0][1], b"hello")
        self.assertIn("name=plan.md", raw_calls[0][0])
        self.assertEqual(result["path"], "/srv/work/upload_files/plan.md")
        self.assertEqual(result["size"], 5)
        self.assertTrue(result["notified"])
        message = [call for call in recorder.calls if call[0] == "/api/relay/message"][0][1]
        self.assertIn("/srv/work/upload_files/plan.md", message["text"])

    def test_send_file_content_chunks_a_large_file(self):
        """2.5MB 的 content 也按 1MB 切块，和 path 走同一条分块上传。"""
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "abc~%3"}})
        calls = []
        plan = [
            {"status": "partial", "offset": 1024 * 1024, "size": 2621440},
            {"status": "partial", "offset": 2 * 1024 * 1024, "size": 2621440},
            {"status": "uploaded", "path": "/srv/work/upload_files/big.bin",
             "size": 2621440},
        ]

        def raw(path, data, **kwargs):
            calls.append((path, data))
            return plan[len(calls) - 1]

        data = bytes(range(256)) * 10240
        self.assertEqual(len(data), 2621440)
        with patch.object(mcp_relay, "request", recorder), \
                patch.object(mcp_relay, "request_raw", raw):
            result = mcp_relay.RelayTools().send_file({
                "target": "abc~%3", "name": "big.bin",
                "content": base64.b64encode(data).decode()})
        self.assertEqual([len(chunk) for _path, chunk in calls],
                         [1024 * 1024, 1024 * 1024, 512 * 1024])
        self.assertEqual(b"".join(chunk for _path, chunk in calls), data)
        self.assertEqual(result["size"], 2621440)
        self.assertTrue(result["notified"])

    def test_send_file_content_writes_an_empty_file(self):
        """空 content 走单次上传，和空 path 一样。"""
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "abc~%3"}})
        raw_calls = []

        def raw(path, data, **kwargs):
            raw_calls.append((path, data))
            return {"status": "uploaded", "path": "/srv/work/upload_files/empty.txt", "size": 0}

        with patch.object(mcp_relay, "request", recorder), \
                patch.object(mcp_relay, "request_raw", raw):
            result = mcp_relay.RelayTools().send_file({
                "target": "abc~%3", "name": "empty.txt",
                "content": base64.b64encode(b"").decode()})
        self.assertEqual(raw_calls[0][1], b"")
        self.assertEqual(result["size"], 0)

    def test_send_file_content_rejects_bad_base64(self):
        """不是合法 base64 的 content 直接报错，和服务端 /mcp 同一个文案。"""
        with self.assertRaises(ValueError) as caught:
            mcp_relay.RelayTools().send_file({
                "target": "abc~%3", "name": "x.bin", "content": "!!!not-base64!!!"})
        self.assertEqual(str(caught.exception), "content is not valid base64")

    def test_send_file_content_requires_a_name(self):
        """content 没有名字可推断（不像 path 有 basename），必须显式给 name。"""
        with self.assertRaises(ValueError) as caught:
            mcp_relay.RelayTools().send_file({
                "target": "abc~%3",
                "content": base64.b64encode(b"hello").decode()})
        self.assertEqual(str(caught.exception), "name is required with content")

    def test_send_file_content_wins_over_path(self):
        """path 和 content 都给了：content 赢，path 碰都不碰（和服务端一致）。"""
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "abc~%3", "handle": "#12", "number": 12, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "abc~%3"}})

        def raw(path, data, **kwargs):
            return {"status": "uploaded", "path": "/srv/work/upload_files/x.bin",
                    "size": len(data)}

        with patch.object(mcp_relay, "request", recorder), \
                patch.object(mcp_relay, "request_raw", raw):
            result = mcp_relay.RelayTools().send_file({
                "target": "abc~%3", "name": "x.bin",
                "path": "/does/not/exist.txt",
                "content": base64.b64encode(b"hello").decode()})
        self.assertEqual(result["size"], 5)
        self.assertTrue(result["notified"])

    def test_send_file_content_over_limit(self):
        """content 超过 TMUXWEB_SEND_FILE_MAX 就拒，和 path 同一个上限。"""
        with patch.dict("os.environ", {"TMUXWEB_SEND_FILE_MAX": "10"}):
            with self.assertRaises(ValueError) as caught:
                mcp_relay.RelayTools().send_file({
                    "target": "abc~%3", "name": "x.bin",
                    "content": base64.b64encode(b"0123456789x").decode()})
        self.assertIn("over the 10 limit", str(caught.exception))

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
        self.assertEqual(set(tools), {"list_agents", "ask_agent", "send_agent", "run_command",
                                       "create_session", "kill_session", "send_file", "read_agent",
                                       "publish_home_card", "list_home", "remove_home_card",
                                       "list_remote_hosts", "add_remote_host", "remove_remote_host"})
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

    def test_rest_errors_carry_the_status_prefix_for_the_unified_tools(self):
        """create_session / kill_session / send_file 的 REST 失败带状态前缀，
        和服务端 /mcp 的 mcp_http_error 同一个格式（"409: ..."）。"""
        cases = [
            ("create_session", {"name": "dev"}, 409, "session already exists: dev"),
            ("kill_session", {"session": "nope"}, 404, "no such session: nope"),
        ]
        for name, arguments, status, message in cases:
            def failing(path, payload=None, *, timeout=30.0, method=None,
                        _s=status, _m=message):
                raise RelayError(_m, status=_s)

            with patch.object(mcp_relay, "request", failing):
                response = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                               "params": {"name": name, "arguments": arguments}})
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(payload["error"], "%s: %s" % (status, message))

        # send_file：peers 查询失败会被吞掉（目录回退为空），真正逃出来的是
        # 上传那一步的 REST 失败，同样带前缀。
        recorder = Recorder({"/api/relay/peers": {"peers": [
            {"id": "%7", "handle": "#4", "number": 4, "path": "/srv/work"}]},
            "/api/relay/message": {"pane": "%7"}})

        def failing_raw(path, data, **kwargs):
            raise RelayError("agent not installed on this host: hermes", status=422)

        with patch.object(mcp_relay, "request", recorder), \
                patch.object(mcp_relay, "request_raw", failing_raw):
            response = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                           "params": {"name": "send_file",
                                                      "arguments": {
                                                          "target": "%7", "name": "x.bin",
                                                          "content": base64.b64encode(b"hi").decode()}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(payload["error"], "422: agent not installed on this host: hermes")

    def test_kill_session_error_matches_the_server_side_text(self):
        """同一失败、两边同一文本：服务端那边的断言在
        tests/test_tmux_web.py test_mcp_kill_session_reports_a_missing_session
       （error 里含 "404" 和 "no such session"），这里钉死逐字相同的文本。"""
        def failing(path, payload=None, *, timeout=30.0, method=None):
            raise RelayError("no such session: nope", status=404)

        with patch.object(mcp_relay, "request", failing):
            response = self.server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                           "params": {"name": "kill_session",
                                                      "arguments": {"session": "nope"}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"], "404: no such session: nope")

    def test_rest_errors_without_a_status_stay_bare(self):
        """连不上 console（status 0）这种没有 HTTP 状态的失败不加前缀。"""
        def failing(path, payload=None, *, timeout=30.0, method=None):
            raise RelayError("cannot reach http://127.0.0.1:8787: refused")

        with patch.object(mcp_relay, "request", failing):
            response = self.server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                           "params": {"name": "kill_session",
                                                      "arguments": {"session": "nope"}}})
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"], "cannot reach http://127.0.0.1:8787: refused")

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
        # hermes has no --yes flag: the two prompts (requires auth? / enable all) are piped in,
        # and the token is seeded as MCP_<NAME>_API_KEY so `mcp add` never asks for it
        self.assertIn("printf 'y\\ny\\n' | hermes mcp add", script)
        self.assertIn('MCP_$(printf', script)
        self.assertIn('--auth header', script)
        # 预置 token 的那段不能把它打到 stdout
        self.assertNotIn('echo "$TOKEN"', script)

    def test_installer_discovers_any_private_network_endpoint(self):
        """地址不限定 Tailscale：EasyTier / ZeroTier / WireGuard / 局域网都算，取第一个真能通的。"""
        script = Path("install_relay_mcp.sh").read_text()
        self.assertIn("scripts/relay-endpoint.sh", script)
        self.assertIn("${DISCOVERED:-http://127.0.0.1:8787}", script)   # 全都不通才回落
        helper = Path("scripts/relay-endpoint.sh").read_text()
        for overlay in ("tailscale", "easytier", "zerotier", "wireguard", "lan", "localhost"):
            self.assertIn(overlay, helper, overlay)
        # macOS 上 tailscale 是 App 里的二进制，不在 PATH
        self.assertIn("/Applications/Tailscale.app/Contents/MacOS/Tailscale", helper)
        # 判据是“能双向访问到 8787”，不是某个产品名
        self.assertIn("200|204|301|302|303|307|308|401|403", helper)
        # 探测必须走直连：有代理的机器上，代理会把其它网段答成 200/502，选错地址
        self.assertIn("--noproxy '*'", helper)
        self.assertIn("--noproxy '*'", script)

    def test_the_endpoint_helper_probes_before_answering(self):
        """--probe 只输出真能连上的那个；全都连不上时退出码 1。"""
        result = subprocess.run(["bash", "scripts/relay-endpoint.sh", "--probe"],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "TMUXWEB_URL": "http://127.0.0.1:9"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "")

        listed = subprocess.run(["bash", "scripts/relay-endpoint.sh", "--list"],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "TMUXWEB_URL": "http://127.0.0.1:8787"})
        self.assertEqual(listed.returncode, 0)
        self.assertEqual(listed.stdout.strip(), "explicit\thttp://127.0.0.1:8787")


class ReverseTunnelTests(unittest.TestCase):
    """反向隧道的脚本：默认参数、常驻模板、不泄露 token。"""

    def run_script(self, *args):
        return subprocess.run(["bash", "scripts/reverse_tunnel.sh", *args],
                              capture_output=True, text=True, timeout=20)

    def test_default_forward_is_loopback_to_loopback(self):
        result = self.run_script("--print-launchd", "user@example", "--ssh-port", "2222")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-R", result.stdout)
        self.assertIn("8787:127.0.0.1:8787", result.stdout)
        self.assertIn("<string>2222</string>", result.stdout)
        self.assertIn("KeepAlive", result.stdout)
        self.assertIn("user@example", result.stdout)

    def test_systemd_template_keeps_the_tunnel_alive(self):
        result = self.run_script("--print-systemd", "user@example", "--remote-port", "9000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("9000:127.0.0.1:8787", result.stdout)
        self.assertIn("Restart=always", result.stdout)
        self.assertIn("ExitOnForwardFailure=yes", result.stdout)

    def test_script_refuses_to_run_without_a_target(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing target", result.stderr)

    def test_the_tunnel_script_never_embeds_a_token(self):
        script = Path("scripts/reverse_tunnel.sh").read_text()
        self.assertNotIn("TMUXWEB_TOKEN", script)
        self.assertNotIn("Authorization", script)


class InstallerTransportTests(unittest.TestCase):
    """默认顺序：tailnet 可达 → HTTP；不可达且给了 --hub → stdio over ssh。"""

    def run_installer(self, *args, env_extra=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": os.environ.get("HOME", "/tmp"),
               "TMUX_WEB_API_TOKEN": "test-token", "TMUX_WEB_MCP_AUTH": "on"}
        env.update(env_extra or {})
        return subprocess.run(["bash", "install_relay_mcp.sh", *args],
                              capture_output=True, text=True, timeout=40, env=env)

    def test_reachable_endpoint_keeps_http(self):
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode: http (", result.stdout)

    def test_unreachable_with_a_hub_switches_to_stdio_over_ssh(self):
        result = self.run_installer("--dry-run", "--assume-unreachable",
                                    "--hub", "me@hub.example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode: stdio over ssh (hub me@hub.example", result.stdout)
        self.assertIn("/usr/bin/ssh me@hub.example", result.stdout)
        # 注册用的是 ssh + wrapper，而不是 URL
        self.assertIn("hermes mcp add tmuxweb --command", result.stdout)

    def test_unreachable_without_a_hub_warns_but_stays_http(self):
        result = self.run_installer("--dry-run", "--assume-unreachable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not reachable from here", result.stderr)
        self.assertIn("mode: http (", result.stdout)


class MissingTokenTests(unittest.TestCase):
    def test_token_falls_back_to_the_server_variable(self):
        with patch.dict(os.environ, {"TMUXWEB_TOKEN": "", "TMUX_WEB_API_TOKEN": "", "TMUXWEB_URL": ""}, clear=False):
            from relay_client import base_url, token
            with self.assertRaises(RelayError):
                token()
            self.assertEqual(base_url(), "http://127.0.0.1:8787")
        with patch.dict(os.environ, {"TMUX_WEB_API_TOKEN": "abc"}, clear=False):
            from relay_client import token
class SendAgentSubmittedTests(unittest.TestCase):
    """send_agent must pass the server's submitted verdict through, not hard-code sent."""

    def test_reports_pasted_not_submitted(self):
        recorder = Recorder({"/api/relay/message": {"pane": "abc~%3", "status": "pasted_not_submitted",
                                                    "submitted": False}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().send_agent({"target": "abc~%3", "text": "run the tests"})
        self.assertFalse(result["sent"])
        self.assertFalse(result["submitted"])

    def test_reports_sent_when_submitted(self):
        recorder = Recorder({"/api/relay/message": {"pane": "abc~%3", "status": "sent", "submitted": True}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().send_agent({"target": "abc~%3", "text": "run the tests"})
        self.assertTrue(result["sent"])
        self.assertTrue(result["submitted"])

    def test_old_server_without_submitted_field_still_reports_sent(self):
        recorder = Recorder({"/api/relay/message": {"pane": "abc~%3"}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().send_agent({"target": "abc~%3", "text": "run the tests"})
        self.assertTrue(result["sent"])
        self.assertTrue(result["submitted"])

    def test_reports_retrying_when_the_server_keeps_retrying(self):
        recorder = Recorder({"/api/relay/message": {"pane": "abc~%3", "status": "retrying",
                                                    "submitted": False, "retrying": True}})
        with patch.object(mcp_relay, "request", recorder):
            result = mcp_relay.RelayTools().send_agent({"target": "abc~%3", "text": "run the tests"})
        self.assertFalse(result["sent"])
        self.assertFalse(result["submitted"])
        self.assertTrue(result["retrying"])
        self.assertEqual(result["status"], "retrying")


if __name__ == "__main__":
    unittest.main()
