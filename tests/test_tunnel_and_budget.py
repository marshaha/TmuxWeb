"""零配置隧道 + 性能预算的上限：都在这一份里盯住。"""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tunnel
import tmux_web
from fastapi.testclient import TestClient

from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


class FakeProcess:
    """够像 Popen：poll()/terminate()/wait()/stdout 迭代。"""

    def __init__(self, lines, exit_code=None):
        self.stdout = iter(lines)
        self.returncode = exit_code
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class QuickTunnelTests(unittest.TestCase):
    def test_the_url_comes_from_cloudflareds_own_output(self):
        found = tunnel.URL_RE.search(
            "INF |  https://silly-words-here.trycloudflare.com  |")
        self.assertEqual(found.group(0), "https://silly-words-here.trycloudflare.com")

    def test_a_url_that_looks_like_a_hostname_is_not_mistaken_for_a_tunnel(self):
        self.assertIsNone(tunnel.URL_RE.search("https://example.com/login"))
        self.assertIsNone(tunnel.URL_RE.search("https://evil.trycloudflare.com.attacker.net"))

    def test_the_reader_picks_up_the_url_and_keeps_the_last_error(self):
        instance = tunnel.QuickTunnel(8787)
        process = FakeProcess([
            "INF Requesting new certificate\n",
            "ERR failed to request quick Tunnel: 429\n",
            "INF |  https://ok-here.trycloudflare.com  |\n",
        ])
        instance._drain(process)
        self.assertEqual(instance._url, "https://ok-here.trycloudflare.com")
        self.assertIn("429", instance._error)

    def test_stop_terminates_the_process_and_forgets_the_url(self):
        instance = tunnel.QuickTunnel(8787)
        process = FakeProcess([])
        instance._process = process
        instance._url = "https://x.trycloudflare.com"
        status = instance.stop()
        self.assertTrue(process.terminated)
        self.assertFalse(status["running"])
        self.assertEqual(status["url"], "")

    def test_the_kill_switch_refuses_to_start(self):
        instance = tunnel.QuickTunnel(8787)
        with mock.patch.dict(os.environ, {"TMUX_WEB_TUNNEL": "off"}):
            self.assertFalse(instance.enabled())
            with self.assertRaises(tunnel.TunnelError):
                instance.start()

    def test_a_missing_binary_is_explained_not_guessed(self):
        instance = tunnel.QuickTunnel(8787, binary="definitely-not-installed-xyz")
        with mock.patch.dict(os.environ, {"TMUX_WEB_TUNNEL": "on"}):
            with self.assertRaises(tunnel.TunnelError) as caught:
                instance.start()
        self.assertIn("cloudflared", str(caught.exception))

    def test_tailscale_reports_only_this_hosts_addresses(self):
        payload = ('{"BackendState":"Running","Self":{"HostName":"robo",'
                   '"TailscaleIPs":["100.123.123.3","fd7a::1"]},'
                   '"Peer":{"a":{"TailscaleIPs":["100.99.99.99"]}}}')
        completed = subprocess.CompletedProcess([], 0, payload, "")
        with mock.patch.object(tunnel.shutil, "which", return_value="/usr/bin/tailscale"), \
             mock.patch.object(tunnel.subprocess, "run", return_value=completed):
            info = tunnel._tailscale()
        self.assertEqual(info["addresses"], ["100.123.123.3"],
                         "Peer 的地址是别的机器，列出来会误导")
        self.assertEqual(info["hostname"], "robo")


class TunnelApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app = tmux_web.create_app(
            FakeTmux(), password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        self.client = TestClient(self.app)
        self.client.post("/login", data={"password": "pw"})

    def test_status_is_off_until_asked(self):
        status = self.client.get("/api/tunnel").json()
        self.assertFalse(status["running"])
        self.assertEqual(status["url"], "")
        self.assertTrue(status["enabled"])

    def test_start_is_refused_when_the_kill_switch_is_on(self):
        with mock.patch.dict(os.environ, {"TMUX_WEB_TUNNEL": "off"}):
            response = self.client.post("/api/tunnel/start")
        self.assertEqual(response.status_code, 409)
        self.assertIn("TMUX_WEB_TUNNEL", response.json()["detail"])

    def test_the_settings_panel_offers_it(self):
        settings = self.client.get("/assets/settings.js").text
        self.assertIn("公网地址（零配置）", settings)
        for marker in ("/api/tunnel/start", "/api/tunnel/stop", "只改变可达性"):
            self.assertIn(marker, settings)


class PerformanceBudgetTests(unittest.TestCase):
    """预算得是写下来的数字，并且真的在代码里生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app = tmux_web.create_app(
            FakeTmux(), password="pw", session_secret="s3cret",
            terminal_factory=FakeTerminalFactory(),
            share_db=str(root / "shares.db"),
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        )
        self.client = TestClient(self.app)
        self.client.post("/login", data={"password": "pw"})

    def test_the_budget_is_a_number_in_the_sampler(self):
        module = self.client.get("/assets/perf-budget.js").text
        self.assertIn("frameP95: 24", module)
        self.assertIn("longestTask: 120", module)
        self.assertIn("nodes: 12000", module)
        # 采样结果要能落进服务日志（手机上复现卡顿时不用盯着屏幕）
        self.assertIn("/api/client-trace", module)
        self.assertIn("perf fps=", module)

    def test_the_message_cap_follows_the_node_budget(self):
        page = self.client.get("/assets/console-app.js").text
        self.assertIn("TRANSCRIPT_PAGE_MAX=500", page)
        self.assertIn("let budget=9500-(formal.length+pendingTail.length)*14;", page)
        self.assertIn("()=>transcriptBubble(message,true,receipt,senderCall)", page)


if __name__ == "__main__":
    unittest.main()
