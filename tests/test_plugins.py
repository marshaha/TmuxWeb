"""The plugin system: discovery, enable/disable, status reporting, failure isolation."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from plugin_api import API_VERSION, PluginManager, PluginServices, parse_enabled  # noqa: E402
from test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux  # noqa: E402
from tmux_web import create_app  # noqa: E402

PASSWORD = "unit-test-admin-password"
PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


def make_app(**kwargs):
    options = dict(password=PASSWORD, session_secret="test-secret", terminal_factory=FakeTerminalFactory(),
                   share_db=":memory:", home_db=":memory:", remote_store=FakeRemoteStore(),
                   remote_runner_factory=FakeSSHRunner,
                   # never touch the deployment's plugins.json from a test
                   plugin_state=str(Path(tempfile.mkdtemp()) / "plugins.json"))
    options.update(kwargs)
    return TestClient(create_app(FakeTmux(), **options))


class ParseEnabledTests(unittest.TestCase):
    def test_the_variable_reads_like_a_list_with_exclusions(self):
        self.assertEqual(parse_enabled(None), (set(), set(), False))
        self.assertEqual(parse_enabled(""), (set(), set(), False))
        self.assertEqual(parse_enabled("none"), (set(), {"*"}, False))
        self.assertEqual(parse_enabled("all"), (set(), set(), True))
        self.assertEqual(parse_enabled("a, b"), ({"a", "b"}, set(), False))
        on, off, load_all = parse_enabled("all,-a")
        self.assertEqual((on, off, load_all), (set(), {"a"}, True))


class PluginLoadingTests(unittest.TestCase):
    def test_first_party_plugins_load_and_the_rest_wait(self):
        client = make_app()
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        status = client.get("/api/plugins").json()
        self.assertEqual(status["api"], API_VERSION)
        by_name = {row["name"]: row for row in status["plugins"]}
        self.assertIn("relay", by_name)
        self.assertTrue(by_name["relay"]["enabled"])
        self.assertTrue(by_name["relay"]["first_party"])
        self.assertIn("example", by_name)                  # shipped, but not enabled
        self.assertFalse(by_name["example"]["enabled"])
        self.assertEqual(by_name["example"]["error"], "")

    def test_the_relay_endpoints_come_from_the_plugin(self):
        client = make_app()
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(client.get("/api/relay/log").status_code, 200)
        # …and they are gone when the plugin is switched off, without touching the core
        off = make_app(plugins="none")
        off.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(off.get("/api/relay/log").status_code, 404)
        self.assertEqual(sorted(off.get("/api/plugins").json()["plugins"][i]["enabled"]
                                for i in range(len(off.get("/api/plugins").json()["plugins"]))), [False, False])

    def test_a_plugin_can_be_turned_on_explicitly(self):
        client = make_app(plugins="example")
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        payload = client.get("/api/example/ping").json()
        self.assertEqual(payload["plugin"], "example")
        self.assertEqual(payload["greeting"], "hello")
        self.assertEqual(client.get("/api/relay/log").status_code, 404)   # only what was asked for

    def test_the_plugin_endpoint_requires_a_login(self):
        client = make_app(plugins="example")
        self.assertEqual(client.get("/api/example/ping").status_code, 401)
        self.assertEqual(client.get("/api/plugins").status_code, 401)

    def test_config_json_and_the_env_override_are_merged(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        plugin = Path(folder.name) / "greeter"
        plugin.mkdir()
        (plugin / "plugin.toml").write_text(
            'name = "greeter"\nversion = "0.1.0"\napi = 1\nentry = "plugin.py"\n', encoding="utf-8")
        (plugin / "plugin.py").write_text(
            "from fastapi import Request\n"
            "from plugin_api import Plugin\n"
            "class Greeter(Plugin):\n"
            "    def setup(self, ctx):\n"
            "        @ctx.app.get('/api/greeter')\n"
            "        async def hello(request: Request):\n"
            "            ctx.require_auth(request)\n"
            "            return {'greeting': ctx.config.get('greeting'), 'extra': ctx.config.get('extra')}\n",
            encoding="utf-8")
        (plugin / "config.json").write_text('{"greeting": "hi"}', encoding="utf-8")
        with patch.dict(os.environ, {"TMUX_WEB_PLUGIN_GREETER_CONFIG": '{"extra": "from-env"}'}):
            client = make_app(plugins_dir=folder.name, plugins="greeter")
            client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            self.assertEqual(client.get("/api/greeter").json(), {"greeting": "hi", "extra": "from-env"})

    def test_a_broken_plugin_is_reported_and_never_breaks_the_app(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        broken = Path(folder.name) / "broken"
        broken.mkdir()
        (broken / "plugin.toml").write_text('name = "broken"\napi = 1\n', encoding="utf-8")
        (broken / "plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        nameless = Path(folder.name) / "nameless"
        nameless.mkdir()                                   # no plugin.toml at all

        client = make_app(plugins_dir=folder.name, plugins="all")
        self.assertEqual(client.get("/login").status_code, 200)      # the app still boots
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        rows = {row["name"]: row for row in client.get("/api/plugins").json()["plugins"]}
        self.assertFalse(rows["broken"]["enabled"])
        self.assertIn("boom", rows["broken"]["error"])
        self.assertFalse(rows["nameless"]["enabled"])
        self.assertIn("plugin.toml", rows["nameless"]["error"])

    def test_a_manifest_naming_another_directory_is_rejected(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        plugin = Path(folder.name) / "right-name"
        plugin.mkdir()
        (plugin / "plugin.toml").write_text('name = "other-name"\napi = 1\n', encoding="utf-8")
        (plugin / "plugin.py").write_text("from plugin_api import Plugin\n", encoding="utf-8")
        client = make_app(plugins_dir=folder.name, plugins="all")
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        rows = client.get("/api/plugins").json()["plugins"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["enabled"])
        self.assertIn("does not match", rows[0]["error"])

    def test_a_newer_api_version_is_refused(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        plugin = Path(folder.name) / "future"
        plugin.mkdir()
        (plugin / "plugin.toml").write_text('name = "future"\napi = 99\n', encoding="utf-8")
        (plugin / "plugin.py").write_text("from plugin_api import Plugin\n", encoding="utf-8")
        client = make_app(plugins_dir=folder.name, plugins="all")
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        row = client.get("/api/plugins").json()["plugins"][0]
        self.assertFalse(row["enabled"])
        self.assertIn("api", row["error"].lower())

    def test_plugins_can_be_switched_at_runtime(self):
        client = make_app()
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(client.get("/api/relay/log").status_code, 200)

        off = client.post("/api/plugins/relay/disable").json()["plugin"]
        self.assertFalse(off["enabled"])
        self.assertEqual(client.get("/api/relay/log").status_code, 404)     # guarded, not unmounted
        self.assertEqual(client.get("/").status_code, 200)                 # everything else keeps working

        on = client.post("/api/plugins/relay/enable").json()["plugin"]
        self.assertTrue(on["enabled"])
        self.assertEqual(client.get("/api/relay/log").status_code, 200)

    def test_a_plugin_can_be_enabled_for_the_first_time_at_runtime(self):
        client = make_app()
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(client.get("/api/example/ping").status_code, 404)
        self.assertTrue(client.post("/api/plugins/example/enable").json()["plugin"]["enabled"])
        self.assertEqual(client.get("/api/example/ping").status_code, 200)
        self.assertTrue(client.post("/api/plugins/example/disable").json()["plugin"]["enabled"] is False)
        self.assertEqual(client.get("/api/example/ping").status_code, 404)

    def test_the_switch_persists_across_restarts(self):
        state = str(Path(tempfile.mkdtemp()) / "plugins.json")
        first = make_app(plugin_state=state)
        first.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        first.post("/api/plugins/relay/disable")
        self.assertIn('"relay": false', Path(state).read_text())

        second = make_app(plugin_state=state)
        second.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(second.get("/api/relay/log").status_code, 404)

    def test_switching_needs_a_login_and_an_existing_plugin(self):
        client = make_app()
        self.assertEqual(client.post("/api/plugins/relay/disable").status_code, 401)
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(client.post("/api/plugins/nope/enable").status_code, 404)

    def test_the_settings_panel_carries_the_plugin_manager(self):
        script = self.client_public().get("/assets/settings.js").text
        self.assertIn('id="ts-plugins"', script)
        self.assertIn("function renderPlugins(", script)
        self.assertIn("/api/plugins/${encodeURIComponent(name)}/${wanted ? 'enable' : 'disable'}", script)
        self.assertIn("role=\"switch\"", script)

    def client_public(self):
        return make_app()

    def test_a_plugin_can_register_an_mcp_tool_a_button_and_assets(self):
        client = make_app()          # example is off by default: nothing of it may leak
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        tools = lambda: [row["name"] for row in                                     # noqa: E731
                         client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                         .json()["result"]["tools"]]
        self.assertNotIn("example_status", tools())
        self.assertEqual(client.get("/api/plugins").json()["buttons"], [])
        self.assertEqual(client.get("/plugin-assets/example/index.html").status_code, 404)

        client.post("/api/plugins/example/enable")
        self.assertIn("example_status", tools())
        call = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": "example_status", "arguments": {"echo": "hi"}}})
        self.assertIn("\"echo\": \"hi\"", call.json()["result"]["content"][0]["text"])
        buttons = client.get("/api/plugins").json()["buttons"]
        self.assertEqual([(row["plugin"], row["href"]) for row in buttons],
                         [("example", "/plugin-assets/example/index.html")])
        self.assertEqual(client.get("/plugin-assets/example/index.html").status_code, 200)

        client.post("/api/plugins/example/disable")
        self.assertNotIn("example_status", tools())
        self.assertEqual(client.get("/api/plugins").json()["buttons"], [])
        self.assertEqual(client.get("/plugin-assets/example/index.html").status_code, 404)

    def test_plugin_assets_cannot_escape_their_folder(self):
        client = make_app(plugins="example")
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(client.get("/plugin-assets/example/../plugin.py").status_code, 404)
        self.assertEqual(client.get("/plugin-assets/nope/index.html").status_code, 404)
        self.assertEqual(client.get("/plugin-assets/example/index.html").status_code, 200)

    def test_the_sidebar_script_is_served_and_wired_into_every_page(self):
        client = make_app()
        script = client.get("/assets/plugins-ui.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("data-plugin-button", script.text)
        self.assertIn("tmuxweb:plugins-changed", script.text)
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        for page in ("/", "/home", "/grid"):
            body = client.get(page).text
            self.assertIn("/assets/plugins-ui.js", body, page)
            self.assertIn('id="i-plug"', body, page)

    def test_capabilities_must_be_declared_in_the_manifest(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        plugin = Path(folder.name) / "greedy"
        plugin.mkdir()
        (plugin / "plugin.toml").write_text('name = "greedy"\nversion = "1"\napi = 1\n', encoding="utf-8")
        (plugin / "plugin.py").write_text(
            "from plugin_api import Plugin\n"
            "class Greedy(Plugin):\n"
            "    def setup(self, ctx):\n"
            "        ctx.add_mcp_tool('greedy_tool', 'x', {'type': 'object'}, lambda a: {})\n",
            encoding="utf-8")
        client = make_app(plugins_dir=folder.name, plugins="all")
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        row = client.get("/api/plugins").json()["plugins"][0]
        self.assertFalse(row["enabled"])
        self.assertIn("mcp", row["error"])
        self.assertIn("permission", row["error"])
        # and the tool never made it into the MCP surface
        names = [t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                              "method": "tools/list"}).json()["result"]["tools"]]
        self.assertNotIn("greedy_tool", names)

    def test_declared_permissions_are_reported(self):
        client = make_app()
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        rows = {row["name"]: row for row in client.get("/api/plugins").json()["plugins"]}
        self.assertEqual(sorted(rows["example"]["permissions"]), ["assets", "mcp", "ui"])
        self.assertEqual(rows["relay"]["permissions"], [])

    def test_assets_need_the_assets_permission(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        plugin = Path(folder.name) / "noassets"
        plugin.mkdir()
        (plugin / "static").mkdir()
        (plugin / "static" / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")
        (plugin / "plugin.toml").write_text('name = "noassets"\nversion = "1"\napi = 1\n', encoding="utf-8")
        (plugin / "plugin.py").write_text("from plugin_api import Plugin\nclass P(Plugin):\n"
                                          "    def setup(self, ctx): pass\n", encoding="utf-8")
        client = make_app(plugins_dir=folder.name, plugins="all")
        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        self.assertEqual(client.get("/plugin-assets/noassets/page.html").status_code, 403)

    def test_themes_are_served_and_offered_in_the_panel(self):
        client = make_app()
        css = client.get("/assets/theme.css")
        script = client.get("/assets/theme.js")
        self.assertEqual((css.status_code, script.status_code), (200, 200))
        for name in ("chatgpt", "claude", "github", "claude-code", "night"):
            self.assertIn(f'[data-theme="{name}"]', css.text)
        self.assertIn("tmux-theme", script.text)
        self.assertIn("TmuxTheme", script.text)

        client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        for page in ("/", "/home", "/grid", "/login"):
            body = client.get(page).text
            self.assertIn("/assets/theme.css", body, page)
            self.assertIn("/assets/theme.js", body, page)

        panel = client.get("/assets/settings.js").text
        self.assertIn('id="ts-themes"', panel)
        self.assertIn("chooseTheme", panel)
        self.assertIn("window.TmuxTheme.set(name)", panel)

    def test_shutdown_hooks_run_in_reverse_order(self):
        calls = []

        class Recorder:
            require_auth = staticmethod(lambda request: None)
            plugins_root = str(PLUGINS_DIR)
            data_root = tempfile.mkdtemp()

        manager = PluginManager(None, PluginServices(**{k: getattr(Recorder, k) for k in
                                                        ("require_auth", "plugins_root", "data_root")}),
                                directory=str(PLUGINS_DIR), enabled="none")
        manager.discover()
        from plugin_api import Plugin, PluginContext
        import logging

        class Two(Plugin):
            def setup(self, ctx):
                ctx.on_shutdown(lambda: calls.append("first registrated, runs last"))
                ctx.on_shutdown(lambda: calls.append("second registrated, runs first"))

        context = PluginContext(app=None, name="t", version="1", directory=PLUGINS_DIR,
                                config={}, services=manager.services, log=logging.getLogger("t"))
        Two().setup(context)
        context.run_shutdown()
        self.assertEqual(calls, ["second registrated, runs first", "first registrated, runs last"])


if __name__ == "__main__":
    unittest.main()
