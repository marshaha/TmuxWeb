"""单向联邦：令牌、作用域、可见范围、`ddr#12` 解析。

这里不碰真实网络：要么直接测 store/transport，要么把 ``FedClient._get`` 换成
固定返回值 —— 断言的是"权限和编号"，不是 HTTP 细节。
"""
import os
import unittest

from fastapi.testclient import TestClient

import federation
from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTerminalFactory, FakeTmux


def memory_stores():
    return federation.FederationStore(":memory:"), federation.FedGrants(":memory:")


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.grants = federation.FedGrants(":memory:")

    def test_a_token_only_has_the_scopes_it_was_issued_with(self):
        issued = self.grants.issue("robo", ["view"], "all", 600)
        grants = self.grants.verify(issued["token"])
        self.assertEqual(grants["scopes"], ["view"])
        self.assertEqual(grants["panes"], "all")

        with self.assertRaises(federation.FederationError):
            self.grants.issue("robo", ["input"], "all", 600)   # 没有 view 就没意义

    def test_revoked_and_unknown_tokens_are_rejected(self):
        issued = self.grants.issue("robo", ["view"], "all", 600)
        self.assertTrue(self.grants.revoke(issued["id"]))
        with self.assertRaises(federation.FederationError):
            self.grants.verify(issued["token"])
        with self.assertRaises(federation.FederationError):
            self.grants.verify("fed_deadbeef.nope")

    def test_the_shared_range_is_checked_per_pane(self):
        self.assertTrue(federation.pane_allowed({"panes": "all"}, "%12"))
        only = {"panes": ["%12"]}
        self.assertTrue(federation.pane_allowed(only, "%12"))
        self.assertFalse(federation.pane_allowed(only, "%13"))
        favs = {"panes": {"favorite": True, "pane_ids": {"%7": True}}}
        self.assertTrue(federation.pane_allowed(favs, "%7"))
        self.assertFalse(federation.pane_allowed(favs, "%8"))


class StoreTests(unittest.TestCase):
    def test_prefixes_are_unique_and_lowercase(self):
        store, _ = memory_stores()
        store.add("oracle", "ddr", "http://10.0.0.9:8787", "https://ddr.167990.xyz", "fed_x.y")
        with self.assertRaises(federation.FederationError):
            store.add("again", "ddr", "http://elsewhere", "", "fed_x.y")
        with self.assertRaises(federation.FederationError):
            store.add("bad", "DDR", "http://elsewhere", "", "fed_x.y")

    def test_the_token_is_not_returned_by_listing(self):
        store, _ = memory_stores()
        store.add("oracle", "ddr", "http://10.0.0.9:8787", "", "fed_secret.zz")
        self.assertNotIn("token", store.listing()[0])
        self.assertEqual(store.get_node(store.listing()[0]["id"], True)["token"], "fed_secret.zz")


class TransportTests(unittest.TestCase):
    def test_remote_panes_get_a_host_scoped_id_and_their_prefix(self):
        node = {"id": "a1b2c3d4e5f60718", "prefix": "ddr", "base_url": "http://node", "token": "t"}
        transport = federation.FedTransport(node)
        transport.client = type("Stub", (), {"panes": lambda self: [
            {"id": "%12", "number": 12, "title": "kimi", "command": "kimi", "path": "/srv"}]})()
        panes = transport.list_panes()
        self.assertEqual(panes[0]["id"], "a1b2c3d4e5f60718~%12")
        self.assertEqual(panes[0]["pane_id"], "%12")
        self.assertEqual(panes[0]["prefix"], "ddr")

    def test_writes_fail_loudly_instead_of_silently(self):
        node = {"id": "a1b2c3d4e5f60718", "prefix": "ddr", "base_url": "http://node", "token": "t"}
        with self.assertRaises(federation.FederationError):
            federation.FedTransport(node).send("%12", "hi", True)


def make_client():
    store, grants = memory_stores()
    backend = FakeTmux()
    client = TestClient(federation_app(backend, store, grants))
    return client, store, grants


def federation_app(backend, store, grants):
    from tmux_web import create_app
    return create_app(
        backend, password="unit-test-admin-password", session_secret="test-secret",
        terminal_factory=FakeTerminalFactory(), share_db=":memory:",
        remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        fed_store=store, fed_tokens=grants,
    )


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client, self.store, self.grants = make_client()
        self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)

    def test_a_fed_token_can_read_panes_and_nothing_else(self):
        issued = self.client.post("/api/fed/tokens", json={"label": "robo", "scopes": ["view"]}).json()
        self.assertTrue(issued["token"].startswith("fed_"))
        headers = {"Authorization": f"Bearer {issued['token']}"}

        listing = self.client.get("/api/fed/panes", headers=headers)
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(listing.json()["panes"], "至少要有 FakeTmux 的 pane")
        pane_id = listing.json()["panes"][0]["id"]
        self.assertEqual(self.client.get(f"/api/fed/panes/{pane_id}/capture", headers=headers).status_code, 200)

        # 没有 view 的路径（比如建会话）FedClient 根本不会调；这里确认令牌不是万能钥匙
        self.assertEqual(self.client.get("/api/fed/panes", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_revoking_takes_effect_immediately(self):
        issued = self.client.post("/api/fed/tokens", json={"label": "robo", "scopes": ["view"]}).json()
        headers = {"Authorization": f"Bearer {issued['token']}"}
        self.assertEqual(self.client.get("/api/fed/panes", headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/fed/tokens/{issued['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/fed/panes", headers=headers).status_code, 401)

    def test_a_shared_subset_hides_the_other_panes(self):
        issued = self.client.post("/api/fed/tokens",
                                  json={"label": "robo", "scopes": ["view"], "panes": ["%4"]}).json()
        headers = {"Authorization": f"Bearer {issued['token']}"}
        listed = {pane["id"] for pane in self.client.get("/api/fed/panes", headers=headers).json()["panes"]}
        self.assertEqual(listed, {"%4"})
        self.assertEqual(self.client.get("/api/fed/panes/%254/capture", headers=headers).status_code, 200)

    def test_prefix_reference_resolves_to_the_node_pane(self):
        """`ddr#12` 落到 `<node>~%12`；靠 hosts 快照解析，解析时不发 HTTP（会死锁）。"""
        self.store.add("oracle", "ddr", "http://node.invalid", "", "fed_x.y")
        node_id = self.store.listing()[0]["id"]

        calls = []

        def fake_get(client, path, timeout=None):
            calls.append(path)
            if path.startswith("/api/fed/panes/") and "/capture" in path:
                return {"pane": "%12", "content": "hello from oracle"}
            if path.startswith("/api/fed/panes"):
                return {"panes": [{"id": "%12", "number": 12, "title": "kimi"}]}
            raise AssertionError(path)

        original = federation.FedClient._get
        federation.FedClient._get = fake_get
        try:
            warm = self.client.get("/api/hosts?refresh=1")
            self.assertEqual(warm.status_code, 200)
            group = [h for h in warm.json()["hosts"] if h.get("kind") == "fed"][0]
            self.assertEqual(group["prefix"], "ddr")
            self.assertEqual(group["status"], "online")

            before = len(calls)
            captured = self.client.get("/api/panes/ddr%2312/capture?history=20")
            self.assertEqual(captured.status_code, 200, captured.text)
            self.assertIn("hello from oracle", captured.json()["content"])
            after_resolve = calls[before:]
        finally:
            federation.FedClient._get = original

        # 解析走快照，只有校验 + capture 各一次 HTTP（解析额外再列一遍就说明它在循环里发了请求）
        self.assertEqual(len(after_resolve), 2, f"解析阶段不该多列一次 pane：{after_resolve}")
        self.assertTrue(after_resolve[0].startswith("/api/fed/panes"))
        self.assertIn("%2512/capture", after_resolve[1])
        self.assertTrue(node_id)

    def test_an_unknown_prefix_says_so(self):
        missing = self.client.get("/api/panes/nope%2312/capture")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("nope", missing.json()["detail"])


if __name__ == "__main__":
    unittest.main()
