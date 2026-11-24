"""Share permissions and federation routing, with in-memory child and parent servers."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import federation
from tests import test_federation as fed
from tests import test_tmux_web as fixtures
from tmux_web import ensure_share_schema
from remote_hosts import split_pane_ref


class ShareCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.backend = fixtures.FakeTmux()
        self.backend.panes[0]["command"] = "claude"
        self.store, self.tokens = fed.memory_stores()
        app = fed.federation_app(self.backend, self.store, self.tokens)
        self.admin = TestClient(app)
        self.guest = TestClient(app)
        self.admin.post("/login", data={"password": "unit-test-admin-password"})

    def create(self, **overrides):
        payload = dict(pane_id="%4", slug="share-test", expires_in=3600)
        response = self.admin.post("/api/shares", json=payload | overrides)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_existing_links_keep_their_permissions_on_migration(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE shares(id TEXT,allow_input INTEGER)")
        db.execute("INSERT INTO shares VALUES ('legacy',1)")
        ensure_share_schema(db)
        ensure_share_schema(db)
        self.assertEqual(db.execute("SELECT allow_input,allow_model_switch,allow_terminal FROM shares").fetchone(), (1,0,0))
        db.close()

    def test_reconfigure_an_existing_link_and_enforce_each_grant(self):
        share = self.create(allow_input=False, password="fixture")
        api = "/share/share-test/api/"
        permissions = "/api/shares/" + share["id"] + "/permissions"
        flags = dict(allow_model_switch=True, allow_input=False, allow_terminal=True)
        self.assertEqual(self.guest.get(api+"models").status_code, 401)
        self.assertEqual(self.guest.put(permissions, json=flags).status_code, 401)
        self.guest.post("/share/share-test/login", data={"password": "fixture"})
        self.assertEqual(self.guest.get(api+"terminal").status_code, 403)
        self.assertEqual(self.guest.get(api+"models").status_code, 403)
        self.assertEqual(self.admin.put(permissions, json=flags).status_code, 200)
        self.assertEqual(self.guest.get(api+"terminal").json()["content"], self.backend.output)
        model = self.guest.get(api+"models").json()["presets"][0]
        self.assertEqual(self.guest.post(api+"model",json={"model":"missing-option"}).status_code,422)
        self.assertEqual(self.guest.post(api+"model",json={}).status_code,403)
        self.assertEqual(self.guest.post(api+"model",json={"model":model}).status_code,200)
        for route, data in (("input", {"text":"fixture"}),("terminal/input", {"text":"fixture"}),("terminal/key",{"key":"Enter"})):
            self.assertEqual(self.guest.post(api+route,json=data).status_code,403)
        self.assertFalse(self.backend.keys)
        self.admin.put(permissions,json=flags|{"allow_input":True, "allow_model_switch":False})
        self.assertEqual(self.guest.post(api+"terminal/key",json={"key":"Down"}).status_code,200)
        self.assertEqual(self.backend.keys, [("%4","Down")])
        self.assertEqual(self.guest.post(api+"model",json={"model":model}).status_code,403)
        with patch.object(self.backend, "transcript", return_value={"session_id":"replacement"}):
            self.assertEqual(self.guest.get(api+"terminal").status_code,409)
            self.assertEqual(self.guest.post(api+"terminal/key",json={"key":"Enter"}).status_code,409)
            self.assertEqual(self.guest.get(api+"transcript").status_code,409)
        self.admin.post("/api/shares/"+share["id"]+"/stop")
        self.assertEqual(self.guest.get(api+"terminal").status_code,410)

    def test_revoking_input_while_a_guest_waits_for_session_validation(self):
        share = self.create()
        started, release = threading.Event(), threading.Event()
        original = self.backend.transcript
        def slow(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise RuntimeError("fixture timed out")
            return original(*args, **kwargs)
        with patch.object(self.backend, "transcript", side_effect=slow), ThreadPoolExecutor(1) as pool:
            response = pool.submit(self.guest.post, "/share/share-test/api/input", json={"text":"fixture"})
            try:
                self.assertTrue(started.wait(10))
                flags = dict(allow_input=False, allow_model_switch=False, allow_terminal=False)
                self.assertEqual(self.admin.put("/api/shares/"+share["id"]+"/permissions", json=flags).status_code, 200)
            finally:
                release.set()
            self.assertEqual(response.result().status_code, 403)
        self.assertFalse(self.backend.sent)

    def test_picker_only_harness_requires_an_interactive_terminal(self):
        self.backend.panes[0].update(command="kimi", provider="kimi")
        transcript = {"provider":"kimi", "session_id":"fixture-kimi", "messages":[], "mtime":1}
        with patch.object(self.backend, "transcript", return_value=transcript):
            share = self.create(allow_model_switch=True)
            api = "/share/share-test/api/"
            self.assertTrue(self.guest.get(api+"models").json()["picker_only"])
            self.assertEqual(self.guest.post(api+"model", json={}).status_code, 403)
            self.assertEqual(self.guest.post(api+"model", json={"model":"unknown"}).status_code, 422)
            self.assertFalse(self.backend.sent)
            flags = dict(allow_input=True, allow_model_switch=True, allow_terminal=True)
            self.admin.put("/api/shares/"+share["id"]+"/permissions", json=flags)
            self.assertEqual(self.guest.post(api+"model", json={}).status_code, 200)
            self.assertEqual(self.backend.sent, [("%4", "/model", True)])

    def test_same_pane_number_on_two_child_hosts_stays_distinct(self):
        node = {"id": "a"*16, "prefix": "child"}
        remote = "b"*16 + "~%4"
        client = Mock()
        client.panes.return_value = [{"id": "%4", "host": "localhost"}, {"id": remote, "host": "build"},
                                     {"id": "%4", "host": "ambiguous legacy entry"}]
        transport = federation.FedTransport(node, client)
        ids = [pane["id"] for pane in transport.list_panes()]
        self.assertEqual(ids, [node["id"]+"~%4", node["id"]+"~"+remote])
        host, target = split_pane_ref(ids[1])
        self.assertEqual((host, target), (node["id"], remote))
        transport.send_key(target, "Down")
        client.key.assert_called_once_with(remote, "Down")

    def test_federated_link_uses_child_models_and_obeys_child_terminal_policy(self):
        backend = fixtures.FakeTmux()
        backend.panes[0]["command"] = "claude"
        store, tokens = fed.memory_stores()
        policy = fed.memory_policy()
        child = TestClient(fed.federation_app(backend,store,tokens,policy))
        token = tokens.issue("parent",["view","admin","input"],["%4"],600)["token"]
        node = self.store.add("Child","child","http://child.invalid","",token)
        headers = {"Authorization":"Bearer "+token}
        def call(_, method, path, data=None, *args, **kwargs):
            resp=child.request(method,path,headers=headers,json=json.loads(data) if data else None)
            if resp.status_code in (401, 403):
                raise federation.FederationAccessError("child denied access")
            if not resp.is_success:
                raise federation.FederationError(str(resp.status_code))
            return resp.json()
        with ExitStack() as stack:
            stack.enter_context(patch.object(federation.FedClient,"_call",call))
            stack.enter_context(patch.object(federation.FedClient,"_get",lambda obj,path,**kw:call(obj,"GET",path)))
            pane = next(h for h in self.admin.get("/api/hosts").json()["hosts"] if h["kind"]=="fed")["panes"][0]
            self.assertTrue(pane["capabilities"]["share"])
            self.assertEqual(pane["id"],node["id"]+"~%4")
            self.create(pane_id=pane["id"],allow_model_switch=True,allow_terminal=True)
            api="/share/share-test/api/"
            self.assertEqual(self.guest.get(api+"transcript").json()["session_id"],"session-1")
            model=self.guest.get(api+"models").json()["presets"][0]
            self.assertEqual(self.guest.get(api+"terminal").status_code,403)
            self.assertEqual(self.guest.post(api+"terminal/key",json={"key":"Enter"}).status_code,403)
            child.post("/login",data={"password":"unit-test-admin-password"})
            child.post("/api/fed/policy",json={"admin":True,"input":True})
            self.assertEqual(self.guest.get(api+"terminal").json()["content"],backend.output)
            self.assertEqual(self.guest.post(api+"model",json={"model":model}).status_code,200)
            self.assertEqual(self.guest.post(api+"terminal/key",json={"key":"Down"}).status_code,200)
            self.assertEqual(self.guest.post(api+"terminal/input",json={"text":"fixture"}).status_code,200)
            self.assertEqual(backend.keys,[("%4","Down")])
            self.assertEqual(backend.sent[-1],("%4","fixture",True))
            self.assertFalse(self.backend.keys)
            self.assertFalse(self.backend.sent)
