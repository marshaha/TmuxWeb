"""transcript 适配器：注册一个就能用，且和 providers/ 注册表不许漂。

这个模块会被 `python3 -` 管道到远端主机，所以它的表必须在文件里自洽；
`providers/` 是服务端的单一真相。两边的一致性靠这里的断言盯着。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers
import transcript_reader
import tempfile

import tmux_web
from fastapi.testclient import TestClient

from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


class AdapterRegistryTests(unittest.TestCase):
    def tearDown(self):
        transcript_reader.ADAPTERS.pop("fake", None)

    def test_every_provider_has_an_adapter_in_the_same_order(self):
        self.assertEqual([a.name for a in transcript_reader.adapters()],
                         list(providers.names()))
        self.assertEqual(transcript_reader.PROVIDERS, providers.names())

    def test_the_adapter_agrees_with_the_registry_about_detection(self):
        for spec in providers.all_specs():
            adapter = transcript_reader.ADAPTERS[spec.name]
            with self.subTest(provider=spec.name):
                self.assertEqual(tuple(adapter.command_prefixes), tuple(spec.command_prefixes))
                # 两边对同一条命令必须给出同一个结论
                for prefix in spec.command_prefixes:
                    self.assertEqual(transcript_reader.provider_for_command(prefix + " --help"),
                                     spec.name)
                    self.assertEqual(providers.provider_for_command(prefix + " --help"), spec.name)

    def test_registry_order_decides_argv_matching(self):
        for argv in ("codex", "/opt/homebrew/bin/codex", "node /x/@openai/codex/bin/codex.js",
                     "kimi-code", "hermes-agent", "claude-code", "python3"):
            with self.subTest(argv=argv):
                self.assertEqual(transcript_reader.provider_from_argv(argv),
                                 providers.provider_from_argv(argv))

    def test_only_the_adapters_that_need_newest_first_are_reversed(self):
        """远端 tail 是 oldest-first：谁要反转是声明出来的，不是猜的。"""
        reversed_names = {a.name for a in transcript_reader.adapters() if a.remote_reversed}
        self.assertEqual(reversed_names, {"claude", "kimi"})

    def test_hermes_has_two_payload_formats_and_sqlite_models(self):
        hermes = transcript_reader.ADAPTERS["hermes"]
        self.assertEqual(sorted(hermes.payload_parsers), ["hermes-jsonl", "hermes-rows"])
        self.assertEqual(hermes.model_source, "sqlite")
        # 其余三个模型都写在文件里
        self.assertEqual({a.model_source for a in transcript_reader.adapters()
                          if a.name != "hermes"}, {"tail"})

    def test_registering_a_new_cli_needs_no_other_branch(self):
        """这就是"适配器化"的承诺：注册之后识别、解析、远端都认它。"""
        seen = {}

        def fake_read(home, cwd, title, limit, hint=None):
            seen["read"] = True
            return {"provider": "fake", "messages": [{"id": "1", "role": "user", "text": "hi"}],
                    "path": "", "session_id": "s1", "model": "fake-1"}

        def fake_parse(lines, limit, session_id="", mtime=0):
            return {"provider": "fake", "messages": [
                {"id": "1", "role": "user", "text": "|".join(lines)}], "session_id": session_id}

        transcript_reader.register(transcript_reader.Adapter(
            "fake", fake_read, fake_parse,
            command_prefixes=("fakecli",), argv_names=("fakecli",),
            remote_reversed=True, model_source="none", note="测试用"))

        self.assertEqual(transcript_reader.provider_for_command("fakecli --chat"), "fake")
        self.assertEqual(transcript_reader.provider_from_argv("/usr/bin/fakecli"), "fake")
        with self.assertRaises(ValueError):
            transcript_reader.register(transcript_reader.Adapter(
                "fake", fake_read, fake_parse))            # 重名必须显式 replace

        data = transcript_reader.read_transcript("fake", "/tmp", "t", limit=5, hint="h")
        self.assertTrue(seen.get("read"))
        self.assertEqual(data["model"], "fake-1")
        self.assertEqual(data["messages"][0]["text"], "hi")

        parsed = transcript_reader.parse_remote_payload("fake", "", "a\nb", 5, "s1")
        self.assertEqual(parsed["messages"][0]["text"], "b|a", "remote_reversed 生效")

    def test_unknown_provider_and_unknown_format_are_loud(self):
        with self.assertRaises(transcript_reader.TranscriptError):
            transcript_reader.adapter_for("nope")
        # 普通格式（jsonl / plain / 空）按 provider 默认解析器走，和以前一致
        transcript_reader.parse_remote_payload("claude", "jsonl", "", 5)
        # 但把 hermes 专属格式丢给别的 provider 要炸：那是调用方认错了对象
        with self.assertRaises(transcript_reader.TranscriptError):
            transcript_reader.parse_remote_payload("claude", "hermes-rows", "[]", 5)
        with self.assertRaises(transcript_reader.TranscriptError):
            transcript_reader.read_transcript("nope", "/tmp", "t")

    def test_summary_is_json_friendly(self):
        summary = transcript_reader.ADAPTERS["claude"].summary()
        self.assertEqual(summary["name"], "claude")
        self.assertIn("command_prefixes", summary)
        self.assertIsInstance(summary["payload_formats"], list)


class AdapterApiTests(unittest.TestCase):
    def test_the_server_lists_its_adapters(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        app = tmux_web.create_app(
            FakeTmux(), password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        client = TestClient(app)
        client.post("/login", data={"password": "pw"})
        payload = client.get("/api/transcript/adapters").json()["adapters"]
        names = [item["name"] for item in payload]
        self.assertEqual(names, list(providers.names()))
        hermes = next(item for item in payload if item["name"] == "hermes")
        self.assertEqual(hermes["model_source"], "sqlite")
        self.assertIn("hermes-rows", hermes["payload_formats"])


if __name__ == "__main__":
    unittest.main()
