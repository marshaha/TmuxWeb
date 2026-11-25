"""Two in-process apps share only display evidence for the granted pane."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_prompts
import federation
from tests.test_cursor_notice import ROWS, frame
from tests.test_federation import federation_app, make_client, memory_stores
from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux
from tmux_web import create_app


class FedMenuTests(unittest.TestCase):
    def test_view_only_parent_reads_live_choices_from_child(self):
        backend = FakeTmux()
        backend.panes[0].update(command="kimi", provider="kimi", pane_pid="123")
        backend.output = "\n".join(ROWS)
        backend.capture_ansi = lambda *args, **kwargs: backend.output
        cursor = [1]
        backend.cursor_frame = lambda actual: frame(y=cursor[0])
        store, grants = memory_stores()
        child = TestClient(federation_app(backend, store, grants))
        headers = {"Authorization": "Bearer " + grants.issue("parent", ["view"], ["%4"], 600)["token"]}
        path = "/api/fed/panes/%254/cursor-menu"
        self.assertEqual(child.get(path).status_code, 401)
        self.assertEqual(child.get(path.replace("254", "255"), headers=headers).status_code, 403)
        evidence = {"question": ROWS[0], "choices": ["Local", "Remote"], "selected": 0}
        self.assertEqual(child.get(path, headers=headers).json(), {"pane": "%4", "menu": evidence})

        parent, nodes, _ = make_client()
        parent.post("/login", data={"password": "unit-test-admin-password"})
        nodes.add("child", "child", "http://fixture.invalid", "", "fed_fixture")
        node = nodes.listing()[0]["id"]
        route = f"/api/panes/{node}~%254/tail?color=1"
        def get(client, path, timeout=None):
            reply = child.get(path, headers=headers)
            reply.raise_for_status()
            return reply.json()
        with patch.object(federation.FedClient, "_get", get):
            data = parent.get(route).json()["cursor_menu_notice"]
            self.assertEqual(data["choices"], evidence["choices"])
            self.assertEqual(data["options"], [])
            self.assertTrue(data["read_only"])
            cursor[0] = 5
            self.assertNotIn("cursor_menu_notice", parent.get(route).json())
        self.assertFalse(backend.keys or backend.sent)

    def test_child_owned_ssh_pane_keeps_its_host_and_grant(self):
        hosts = FakeRemoteStore()
        host = hosts.add_host(name="fixture", credential="dummy")
        scoped = host["id"] + "~%4"
        class Runner(FakeSSHRunner):
            def list_panes(self, timeout=15):
                return [{"id": scoped, "pane_id": "%4", "host_id": host["id"], "pane_pid": "123",
                         "provider": "kimi", "command": "kimi", "session": "fixture"}]

            def cursor_frame(self, actual):
                if actual != "%4":
                    raise AssertionError(actual)
                return frame()

        store, grants = memory_stores()
        child = TestClient(create_app(FakeTmux(), password="fixture", session_secret="fixture",
                                      terminal_factory=FakeTerminalFactory(), remote_store=hosts,
                                      remote_runner_factory=Runner, share_db=":memory:",
                                      fed_store=store, fed_tokens=grants))
        token = grants.issue("parent", ["view"], [scoped], 600)["token"]
        headers = {"Authorization": "Bearer " + token}
        listing = child.get("/api/fed/panes", headers=headers).json()["panes"]
        self.assertEqual([row["id"] for row in listing], [scoped])
        self.assertTrue(listing[0]["capabilities"]["cursor_menu"])
        saved = child.get("/api/fed/panes/" + scoped.replace("%", "%25") + "/cursor-menu", headers=headers)
        self.assertEqual(saved.json()["pane"], scoped)
        self.assertEqual(saved.json()["menu"]["choices"], ["Local", "Remote"])
        self.assertEqual(child.get("/api/fed/panes/%254/cursor-menu", headers=headers).status_code, 403)

    def test_scoped_target_and_display_fields(self):
        client = federation.FedClient("http://fixture.invalid", "", "dummy")
        pane = "a1b2c3d4e5f60718~%4"
        with patch.object(client, "_get", return_value={"pane": "%4", "menu": {}}) as get:
            self.assertIsNone(client.cursor_menu(pane))
            get.assert_called_once_with("/api/fed/panes/a1b2c3d4e5f60718~%254/cursor-menu")
        evidence = {"question": "Choose?", "choices": ["<literal>", "Other"], "selected": 0,
                    "source": "hermes_clarify", "verified": True, "read_only": False, "keys": ["Enter"]}
        notice = agent_prompts.cursor_notice_from_evidence(evidence)
        self.assertEqual(notice["source"], "cursor_menu_notice")
        self.assertTrue(notice["read_only"])
        self.assertNotIn("verified", notice)
        self.assertNotIn("keys", notice)
        for invalid in ({"selected": -1}, {"selected": True}, {"choices": ["A"] * 21}, {"question": "a\x1bb"}):
            self.assertIsNone(agent_prompts.cursor_notice_from_evidence(dict(evidence, **invalid)))
