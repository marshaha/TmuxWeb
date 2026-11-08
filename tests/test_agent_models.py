"""agent_models: which agents expose a model picker, and what to type."""

import unittest

import agent_models


class SwitchCommandTests(unittest.TestCase):
    def test_argument_form_for_agents_that_take_one(self):
        self.assertEqual(agent_models.switch_command("claude", "opus"), "/model opus")
        self.assertEqual(agent_models.switch_command("codex", "gpt-5.6-sol"), "/model gpt-5.6-sol")
        self.assertEqual(agent_models.switch_command("hermes", "deepseek-flash"), "/model deepseek-flash")

    def test_switching_across_providers_names_the_provider(self):
        # Hermes 一个 config 里挂好几组 provider，跨组必须带 --provider
        self.assertEqual(agent_models.switch_command("hermes", "gpt-5.6-sol", "ctyun"),
                         "/model gpt-5.6-sol --provider ctyun")
        self.assertEqual(agent_models.switch_command("hermes", "deepseek-flash"),
                         "/model deepseek-flash")
        with self.assertRaises(ValueError):
            agent_models.switch_command("hermes", "x", "bad provider; rm -rf /")

    def test_kimi_opens_its_own_picker(self):
        # `kimi` 回 "Unknown model alias" 给任何参数，只能用无参形式
        self.assertTrue(agent_models.picker_only("kimi"))
        self.assertEqual(agent_models.switch_command("kimi", ""), "/model")
        self.assertEqual(agent_models.switch_command("kimi", "whatever"), "/model")

    def test_missing_or_dirty_model_is_rejected(self):
        with self.assertRaises(ValueError):
            agent_models.switch_command("claude", "   ")
        with self.assertRaises(ValueError):
            agent_models.switch_command("claude", "opus\nrm -rf /")
        with self.assertRaises(ValueError):
            agent_models.switch_command("claude", "x" * 81)

    def test_non_agents_have_no_picker(self):
        with self.assertRaises(ValueError):
            agent_models.switch_command("", "opus")
        with self.assertRaises(ValueError):
            agent_models.switch_command("bash", "opus")
        self.assertFalse(agent_models.supported("bash"))

    def test_presets_only_for_known_providers(self):
        self.assertIn("opus", agent_models.preset_for("claude"))
        self.assertEqual(agent_models.preset_for("bash"), [])


class ProviderLabelTests(unittest.TestCase):
    def test_vendor_labels_and_configured_route(self):
        self.assertEqual(agent_models.provider_label("claude"), "Anthropic")
        self.assertEqual(agent_models.provider_label("codex"), "OpenAI")
        self.assertEqual(agent_models.provider_label("kimi"), "Kimi Code")
        # Hermes 走哪个网关是用户自己的配置，配置了就用它
        self.assertEqual(agent_models.provider_label("hermes", "axera"), "axera")
        self.assertEqual(agent_models.provider_label("hermes"), "Hermes")
        self.assertEqual(agent_models.provider_label(""), "")

    def test_hermes_route_ignores_comments_and_lookalike_keys(self):
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as home:
            config = P(home) / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "# Hermes Agent CLI Configuration\n"
                "providers:\n"
                "  elsewhere:\n"
                "    provider: wrong\n"
                "model:\n"
                "  default: gpt-5.6-sol\n"
                "  # route for this box\n"
                "  provider: \"ctyun\"\n"
                "agent:\n"
                "  max_turns: 500\n"
            )
            self.assertEqual(agent_models.hermes_route(home), "ctyun")
            self.assertEqual(agent_models.hermes_route(home + "-missing"), "")


    def test_groups_cover_every_provider_in_the_config(self):
        import tempfile
        from pathlib import Path as P
        from unittest import mock

        with tempfile.TemporaryDirectory() as home:
            config = P(home) / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(HermesModelListTests.CONFIG)

            def fake_fetch(base_url, key, timeout=8.0):
                return {"https://axera.example/v1": ["a1", "a2"],
                        "https://ctyun.example/v1": ["c1"]}.get(base_url, [])

            with mock.patch.object(agent_models, "fetch_models", fake_fetch), \
                    mock.patch.dict("os.environ", {"CTYUN_API_KEY": "k", "AXERA_API_KEY": "k"}):
                groups = agent_models.hermes_groups(home, route="ctyun")
        self.assertEqual([group["provider"] for group in groups], ["ctyun", "axera"])
        self.assertEqual(groups[0]["models"], ["c1"])
        self.assertEqual(groups[1]["models"], ["a1", "a2"])


class HermesModelListTests(unittest.TestCase):
    """Hermes 没有静态模型表：去 provider 的 /v1/models 问。"""

    CONFIG = (
        "# comment at column 0\n"
        "model:\n"
        "  default: \"gpt-5.6-sol\"\n"
        "  # provider for this box\n"
        "  provider: \"ctyun\"\n"
        "providers:\n"
        "  axera:\n"
        "    name: axera\n"
        "    base_url: https://axera.example/v1\n"
        "    key_env: AXERA_API_KEY\n"
        "  ctyun:\n"
        "    name: ctyun\n"
        "    base_url: https://ctyun.example/v1\n"
        "    key_env: CTYUN_API_KEY\n"
        "agent:\n"
        "  max_turns: 500\n"
    )

    def test_provider_map_reads_names_urls_and_keys(self):
        providers = agent_models.provider_map(self.CONFIG)
        self.assertEqual(sorted(providers), ["axera", "ctyun"])
        self.assertEqual(providers["ctyun"]["base_url"], "https://ctyun.example/v1")
        self.assertEqual(providers["axera"]["key_env"], "AXERA_API_KEY")

    def test_models_come_from_the_providers_endpoint(self):
        import tempfile
        from pathlib import Path as P
        from unittest import mock

        with tempfile.TemporaryDirectory() as home:
            config = P(home) / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(self.CONFIG)
            seen = {}

            def fake_fetch(base_url, key, timeout=8.0):
                seen["base_url"], seen["key"] = base_url, key
                return ["gpt-5.5", "gpt-5.6-sol"]

            with mock.patch.dict("os.environ", {"CTYUN_API_KEY": "k-123"}), \
                    mock.patch.object(agent_models, "fetch_models", fake_fetch):
                models = agent_models.hermes_models(home)
                again = agent_models.hermes_models(home)      # 命中缓存
        self.assertEqual(models, ["gpt-5.5", "gpt-5.6-sol"])
        self.assertEqual(again, models)
        self.assertEqual(seen["base_url"], "https://ctyun.example/v1")
        self.assertEqual(seen["key"], "k-123")

    def test_a_dead_provider_returns_no_models_instead_of_raising(self):
        from unittest import mock
        with mock.patch.object(agent_models, "fetch_models", side_effect=OSError("down")):
            self.assertEqual(agent_models.hermes_models(None, None, "ghost"), [])

    def test_fetch_sends_a_user_agent(self):
        # 有的网关对 python-urllib 的默认 UA 直接 403
        import io
        from unittest import mock

        captured = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["ua"] = request.get_header("User-agent")
            return Response(b'{"data": [{"id": "m1"}, {"id": "m2"}]}')

        import urllib.request as urlrequest
        with mock.patch.object(urlrequest, "urlopen", fake_urlopen):
            self.assertEqual(agent_models.fetch_models("https://x/v1", "k"), ["m1", "m2"])
        self.assertEqual(captured["ua"], "TmuxWeb/1.0")

    def test_private_hosts_are_direct(self):
        # 内网 / tailnet / 回环地址不该走 HTTP 代理：代理会把别的网段答成
        # 200/502，模型列表就被骗成空的或者错的（aff8e08 同款问题）。
        for host in ("192.168.1.10", "10.0.0.8", "172.16.0.5", "100.64.0.5",
                     "127.0.0.1", "models.local", "gw.internal"):
            self.assertTrue(agent_models._is_private_host(host), host)
        for host in ("8.8.8.8", "api.example.com", "models.example.com", ""):
            self.assertFalse(agent_models._is_private_host(host), host)

    def test_fetch_models_bypasses_the_proxy_for_private_hosts(self):
        import io
        from unittest import mock

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeOpener:
            def open(self, request, timeout=None):
                self.request = request
                return Response(b'{"data": [{"id": "m1"}]}')

        opener = FakeOpener()
        import urllib.request as urlrequest
        with mock.patch.object(urlrequest, "build_opener", return_value=opener), \
                mock.patch.object(urlrequest, "urlopen",
                                  side_effect=AssertionError("proxy path used")):
            self.assertEqual(agent_models.fetch_models("http://192.168.1.10:8080/v1", "k"),
                             ["m1"])
        self.assertEqual(opener.request.get_header("User-agent"), "TmuxWeb/1.0")


class RemoteModelListTests(unittest.TestCase):
    def test_route_is_passed_on_the_same_line_as_python(self):
        """脚本常量以换行开头：前缀必须跟 python3 同一行，否则 VARIABLE=x 单独成行不生效。"""
        from unittest import mock

        class Runner:
            host_id = "h1"
            def __init__(self):
                self.commands = []
            def run(self, command, timeout=None):
                self.commands.append(command)
                class Result:
                    returncode = 0
                    stdout = b"axera<::>m1\naxera<::>m2\n"
                    stderr = b""
                return Result()

        runner = Runner()
        models = agent_models._remote_models(runner, "axera")
        self.assertEqual(models, ["m1", "m2"])
        self.assertTrue(runner.commands[0].startswith("TMUXWEB_ROUTE='axera' python3 -"),
                        runner.commands[0][:60])

    def test_remote_script_lists_models_from_a_loopback_gateway(self):
        """远端脚本本身能跑通：内网网关（127.0.0.1）直连取 /models，不走代理。

        脚本是嵌在常量里的 heredoc：抽出 python 部分，配一个假 ~/.hermes/
        config.yaml，起一个本地 HTTP 服务当网关，真实跑一遍。
        """
        import http.server
        import json
        import os
        import socketserver
        import subprocess
        import tempfile
        import threading
        from pathlib import Path

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode())

            def log_message(self, *args):
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as home:
                    hermes = Path(home) / ".hermes"
                    hermes.mkdir()
                    (hermes / "config.yaml").write_text(
                        "model:\n  provider: \"axera\"\nproviders:\n  axera:\n"
                        f"    base_url: \"http://127.0.0.1:{port}/v1\"\n")
                    lines = agent_models.REMOTE_MODELS_SCRIPT.strip().splitlines()
                    self.assertTrue(lines[0].startswith("python3 - <<"))
                    self.assertEqual(lines[-1], "PYX")
                    script = "\n".join(lines[1:-1])
                    env = dict(os.environ, HOME=home)
                    completed = subprocess.run(["python3", "-c", script],
                                               capture_output=True, text=True, env=env, timeout=30)
            finally:
                server.shutdown()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip().splitlines(),
                         ["axera<::>m1", "axera<::>m2"])


if __name__ == "__main__":
    unittest.main()
