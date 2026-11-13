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


def memory_policy():
    return federation.FedPolicy(":memory:")


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


class UserAgentTests(unittest.TestCase):
    def test_requests_identify_themselves_so_cloudflare_does_not_ban_them(self):
        """CF 的浏览器完整性检查封 python-urllib（Error 1010），必须自带 UA。"""
        import io
        import urllib.request

        seen = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *info):
                return False

        def fake_urlopen(request, timeout=None):
            seen.append(request)
            return Reply(b'{"tmuxweb":true,"host":"ddr"}')

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            client = federation.FedClient("https://ddr.example", "", "fed_a.b")
            self.assertTrue(client.ping()["tmuxweb"])
        finally:
            urllib.request.urlopen = original
        self.assertEqual(seen[0].get_header("User-agent"), federation.USER_AGENT)
        self.assertNotIn("urllib", (seen[0].get_header("User-agent") or "").lower())

    def test_a_1010_block_explains_itself(self):
        import urllib.error
        import urllib.request

        def blocked(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                         io_body(b'{"title":"Error 1010: Access denied","status":403}'))

        def io_body(payload):
            import io
            return io.BytesIO(payload)

        original = urllib.request.urlopen
        urllib.request.urlopen = blocked
        try:
            with self.assertRaises(federation.FederationError) as caught:
                federation.FedClient("https://ddr.example", "", "fed_a.b").ping()
        finally:
            urllib.request.urlopen = original
        self.assertIn("Cloudflare", str(caught.exception))


class PermanentTests(unittest.TestCase):
    def test_ttl_zero_is_permanent_and_only_revocation_stops_it(self):
        grants = federation.FedGrants(":memory:")
        issued = grants.issue("robo", ["view"], "all", 0)
        self.assertIsNone(issued["expires_at"])
        self.assertEqual(grants.listing()[0]["expires_at"], None)
        self.assertEqual(grants.verify(issued["token"])["scopes"], ["view"])
        grants.revoke(issued["id"])
        with self.assertRaises(federation.FederationError):
            grants.verify(issued["token"])

    def test_an_expiring_token_still_expires(self):
        grants = federation.FedGrants(":memory:")
        issued = grants.issue("robo", ["view"], "all", 300)
        self.assertGreater(issued["expires_at"], 0)


class PreferTests(unittest.TestCase):
    def test_switching_the_preferred_route_swaps_the_two_addresses(self):
        store, _ = memory_stores()
        node = store.add("oracle", "", "http://100.123.123.17:8787", "https://ddr.167990.xyz", "fed_x.y")
        self.assertEqual((node["base_url"], node["fallback_url"], node["prefer"]),
                         ("http://100.123.123.17:8787", "https://ddr.167990.xyz", "direct"))

        swapped = store.set_prefer(node["id"], "public")
        self.assertEqual((swapped["base_url"], swapped["fallback_url"], swapped["prefer"]),
                         ("https://ddr.167990.xyz", "http://100.123.123.17:8787", "public"))
        # 再切回来就是原样，且不带 token
        back = store.set_prefer(node["id"], "direct")
        self.assertEqual(back["base_url"], "http://100.123.123.17:8787")
        self.assertNotIn("token", back)


class ThreeRouteTests(unittest.TestCase):
    """TS / 局域网 / CF 三条地址：顺序由地址种类决定，可以随时切。"""

    def test_a_new_node_sorts_tailnet_then_lan_then_domain(self):
        store, _ = memory_stores()
        # add 时不排序会被排序：直连优先，tailnet 排在最前
        node = store.add("oracle", "", "https://ddr.167990.xyz", "http://100.123.123.17:8787", "fed_x.y")
        self.assertEqual(node["base_url"], "http://100.123.123.17:8787")
        self.assertEqual(node["routes"], ["tailnet", "public"])

    def test_three_addresses_keep_the_order_they_were_given(self):
        store, _ = memory_stores()
        node = store.add("oracle", "", "https://ddr.167990.xyz", "", "fed_x.y")
        saved = store.set_addresses(node["id"], [
            "https://ddr.167990.xyz",
            "http://10.0.2.12:8787",
            "http://100.123.123.17:8787",
        ])
        # 手填的顺序就是真顺序（页面上点了哪个就存哪个）
        self.assertEqual([entry["kind"] for entry in saved["addresses"]],
                         ["public", "lan", "tailnet"])
        self.assertEqual(saved["base_url"], "https://ddr.167990.xyz")

        # 想换回 tailnet 优先，点一下就行
        moved = store.set_route(node["id"], "tailnet")
        self.assertEqual(moved["base_url"], "http://100.123.123.17:8787")
        self.assertEqual(len(moved["addresses"]), 3, "换顺序不该丢地址")

    def test_asking_for_the_domain_first_moves_it_up_without_losing_the_others(self):
        store, _ = memory_stores()
        node = store.add("oracle", "", "http://100.123.123.17:8787", "https://ddr.167990.xyz", "fed_x.y")
        store.set_addresses(node["id"], [
            {"url": "http://100.123.123.17:8787"},
            {"url": "http://10.0.2.12:8787"},
            {"url": "https://ddr.167990.xyz"},
        ])
        moved = store.set_route(node["id"], "public")
        self.assertEqual(moved["base_url"], "https://ddr.167990.xyz")
        self.assertEqual(moved["prefer"], "public")
        self.assertEqual(len(moved["addresses"]), 3)

        back = store.set_route(node["id"], "lan")
        self.assertEqual(back["base_url"], "http://10.0.2.12:8787")

    def test_a_route_the_node_does_not_have_is_refused(self):
        store, _ = memory_stores()
        node = store.add("oracle", "", "https://ddr.167990.xyz", "", "fed_x.y")
        with self.assertRaises(federation.FederationError):
            store.set_route(node["id"], "tailnet")

    def test_an_old_record_with_only_two_addresses_still_reports_routes(self):
        store, _ = memory_stores()
        node = store.add("oracle", "", "https://ddr.167990.xyz", "http://100.1.2.3:8787", "fed_x.y")
        store.connection.execute("UPDATE fed_nodes SET addresses=NULL WHERE id=?", (node["id"],))
        store.connection.commit()
        again = store.get_node(node["id"])
        self.assertEqual([entry["kind"] for entry in again["addresses"]], ["tailnet", "public"])


class AuthKeyTests(unittest.TestCase):
    def test_an_auth_key_can_carry_the_address_after_the_at_sign(self):
        self.assertEqual(federation.parse_auth_key("fed_a.b@https://ddr.167990.xyz/"),
                         ("fed_a.b", "https://ddr.167990.xyz"))
        self.assertEqual(federation.parse_auth_key("  fed_a.b  "), ("fed_a.b", ""))
        # 不是地址就别拆：令牌里本来没有 @ 之外的花样
        self.assertEqual(federation.parse_auth_key("fed_a.b@nonsense"), ("fed_a.b@nonsense", ""))

    def test_the_prefix_comes_from_the_name(self):
        store, _ = memory_stores()
        node = store.add("Oracle", "", "http://node", "", "fed_x.y")
        self.assertEqual(node["prefix"], "oracle")
        with self.assertRaises(federation.FederationError):
            store.add("中文名", "", "http://node", "", "fed_x.y")


class StoreTests(unittest.TestCase):
    def test_prefixes_are_unique_and_lowercase(self):
        store, _ = memory_stores()
        store.add("oracle", "ddr", "http://10.0.0.9:8787", "https://ddr.167990.xyz", "fed_x.y")
        with self.assertRaises(federation.FederationError):
            store.add("again", "ddr", "http://elsewhere", "", "fed_x.y")
        with self.assertRaises(federation.FederationError):
            store.add("bad", "DDR", "http://elsewhere", "", "fed_x.y")

    def test_adding_a_node_does_not_echo_its_token(self):
        store, _ = memory_stores()
        node = store.add("oracle", "", "https://ddr.167990.xyz", "", "fed_x.y", )
        self.assertNotIn("token", node)

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
    policy = memory_policy()
    backend = FakeTmux()
    client = TestClient(federation_app(backend, store, grants, policy))
    return client, store, grants


def federation_app(backend, store, grants, policy=None):
    from tmux_web import create_app
    return create_app(
        backend, password="unit-test-admin-password", session_secret="test-secret",
        terminal_factory=FakeTerminalFactory(), share_db=":memory:",
        remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        fed_store=store, fed_tokens=grants, policy_store=policy or memory_policy(),
    )


class WriteScopeTests(unittest.TestCase):
    """写操作要 input 作用域，文件要 files；没有就 403，不是悄悄不做。"""

    def setUp(self):
        self.client, self.store, self.grants = make_client()
        self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        # 本机政策默认只开 view（和真实默认一致），这些用例先把要用的能力打开
        self.client.post("/api/fed/policy", json={"input": True, "files": True, "admin": True})
        listing = self.client.get("/api/fed/panes",
                                  headers=self.headers(["view"])).json()
        self.pane = listing["panes"][0]["id"]

    def headers(self, scopes):
        issued = self.client.post("/api/fed/tokens",
                                  json={"label": "t", "scopes": scopes}).json()
        return {"Authorization": f"Bearer {issued['token']}"}

    def test_sending_text_needs_the_input_scope(self):
        view_only = self.headers(["view"])
        refused = self.client.post(f"/api/fed/panes/{self.pane}/input",
                                   json={"text": "ls", "submit": True}, headers=view_only)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("input", refused.text)

        allowed = self.client.post(f"/api/fed/panes/{self.pane}/input",
                                   json={"text": "ls", "submit": True},
                                   headers=self.headers(["view", "input"]))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json()["submitted"])

    def test_only_known_keys_are_accepted(self):
        headers = self.headers(["view", "input"])
        self.assertEqual(self.client.post(f"/api/fed/panes/{self.pane}/key",
                                          json={"key": "Escape"}, headers=headers).status_code, 200)
        self.assertEqual(self.client.post(f"/api/fed/panes/{self.pane}/key",
                                          json={"key": "rm -rf"}, headers=headers).status_code, 422)

    def test_files_need_the_files_scope_and_round_trip(self):
        view_only = self.headers(["view"])
        self.assertEqual(self.client.get("/api/fed/files?path=/tmp", headers=view_only).status_code, 403)

        files = self.headers(["view", "files"])
        import tempfile, os
        with tempfile.TemporaryDirectory() as folder:
            written = self.client.put(f"/api/fed/files/upload?dir={folder}&name=hello.txt",
                                      content=b"hello federation", headers=files)
            self.assertEqual(written.status_code, 200, written.text)
            self.assertEqual(os.path.getsize(os.path.join(folder, "hello.txt")), len(b"hello federation"))

            listed = self.client.get(f"/api/fed/files?path={folder}", headers=files).json()
            self.assertIn("hello.txt", [entry["name"] for entry in listed["entries"]])

            got = self.client.get(f"/api/fed/files/download?path={folder}/hello.txt", headers=files)
            self.assertEqual(got.content, b"hello federation")

            renamed = self.client.post("/api/fed/files/rename",
                                       json={"path": f"{folder}/hello.txt", "new_name": "x.txt"},
                                       headers=files)
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertTrue(os.path.exists(os.path.join(folder, "x.txt")))

            dropped = self.client.post("/api/fed/files/delete",
                                       json={"path": f"{folder}/x.txt"}, headers=files)
            self.assertEqual(dropped.status_code, 200, dropped.text)


class PolicyTests(unittest.TestCase):
    """权限由子节点自己管：关掉之后，连已经签出去的 key 也立刻失效。"""

    def setUp(self):
        self.client, self.store, self.grants = make_client()
        self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)

    def test_writes_are_off_until_this_machine_allows_them(self):
        policy = self.client.get("/api/fed/policy").json()
        self.assertEqual(policy["allow"], {"input": False, "files": False, "admin": False})

        refused = self.client.post("/api/fed/tokens", json={"label": "x", "scopes": ["view", "input"]})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("发消息", refused.text)

        self.client.post("/api/fed/policy", json={"input": True})
        self.assertTrue(self.client.get("/api/fed/policy").json()["allow"]["input"])
        issued = self.client.post("/api/fed/tokens", json={"label": "x", "scopes": ["view", "input"]})
        self.assertEqual(issued.status_code, 201, issued.text)

        # 关掉之后：这把 key 立刻失效（401/403 都算拒绝，命中 403）
        self.client.post("/api/fed/policy", json={"input": False})
        headers = {"Authorization": f"Bearer {issued.json()['token']}"}
        pane = self.client.get("/api/fed/panes", headers=headers).json()["panes"][0]["id"]
        blocked = self.client.post(f"/api/fed/panes/{pane}/input",
                                   json={"text": "ls", "submit": True}, headers=headers)
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("没有开放", blocked.text)

    def test_the_parent_facing_list_hides_nothing_but_flags_are_readable(self):
        self.client.post("/api/fed/policy", json={"files": True})
        labels = self.client.get("/api/fed/policy").json()["labels"]
        self.assertIn("files", labels)


class KeyEditingTests(unittest.TestCase):
    """已经签出去的 key 要能改权限和到期，也要能整条删掉。"""

    def setUp(self):
        self.client, self.store, self.grants = make_client()
        self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.client.post("/api/fed/policy", json={"input": True, "files": True, "admin": True})
        self.issued = self.client.post("/api/fed/tokens",
                                       json={"label": "robo", "scopes": ["view"], "ttl": 600}).json()

    def test_scopes_and_expiry_can_be_changed_after_the_fact(self):
        changed = self.client.patch(f"/api/fed/tokens/{self.issued['id']}",
                                    json={"scopes": ["view", "files"], "ttl": 0})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["scopes"], ["view", "files"])
        self.assertIsNone(changed.json()["expires_at"])
        # 令牌本身不变，权限立刻生效：现在能列文件了
        headers = {"Authorization": f"Bearer {self.issued['token']}"}
        self.assertEqual(self.client.get("/api/fed/files?path=/tmp", headers=headers).status_code, 200)

    def test_editing_cannot_hand_out_a_capability_the_machine_has_off(self):
        self.client.post("/api/fed/policy", json={"files": False})
        refused = self.client.patch(f"/api/fed/tokens/{self.issued['id']}",
                                    json={"scopes": ["view", "files"]})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("没开放", refused.text)

    def test_deleting_the_record_kills_and_forgets_the_key(self):
        gone = self.client.delete(f"/api/fed/tokens/{self.issued['id']}?purge=1")
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertEqual(self.client.get("/api/fed/tokens").json()["tokens"], [])
        headers = {"Authorization": f"Bearer {self.issued['token']}"}
        self.assertEqual(self.client.get("/api/fed/panes", headers=headers).status_code, 401)


class AdminScopeTests(unittest.TestCase):
    def setUp(self):
        self.client, self.store, self.grants = make_client()
        self.client.post("/login", data={"password": "unit-test-admin-password"}, follow_redirects=False)
        self.client.post("/api/fed/policy", json={"admin": True, "files": True})

    def headers(self, scopes):
        issued = self.client.post("/api/fed/tokens", json={"label": "t", "scopes": scopes}).json()
        return {"Authorization": f"Bearer {issued['token']}"}

    def test_creating_and_closing_a_session_needs_admin(self):
        view_only = self.headers(["view"])
        self.assertEqual(self.client.post("/api/fed/sessions", json={"name": "fed-x"},
                                          headers=view_only).status_code, 403)

        admin = self.headers(["view", "admin"])
        created = self.client.post("/api/fed/sessions", json={"name": "fed-x", "path": "/tmp"},
                                   headers=admin)
        self.assertEqual(created.status_code, 201, created.text)

        panes = self.client.get("/api/fed/panes", headers=admin).json()["panes"]
        self.assertTrue(panes)
        closed = self.client.post(f"/api/fed/panes/{panes[0]['id']}/close", headers=admin)
        self.assertEqual(closed.status_code, 200, closed.text)

    def test_zip_returns_a_readable_archive(self):
        import io, tempfile, zipfile, os
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a.txt", "b.txt"):
                with open(os.path.join(folder, name), "w") as handle:
                    handle.write(name)
            files = self.headers(["view", "files"])
            answer = self.client.post("/api/fed/files/zip",
                                      json={"paths": [os.path.join(folder, "a.txt"),
                                                      os.path.join(folder, "b.txt")],
                                            "base": folder}, headers=files)
        self.assertEqual(answer.status_code, 200, answer.text)
        archive = zipfile.ZipFile(io.BytesIO(answer.content))
        self.assertEqual(sorted(archive.namelist()), ["a.txt", "b.txt"])


class RouteChoiceTests(unittest.TestCase):
    def test_files_use_the_same_route_the_user_picked(self):
        """用户说 CF 可能比 tailnet 快 —— 那就别自作聪明，文件和其它请求走同一条。"""
        node = {"id": "a1", "prefix": "ddr", "token": "t",
                "base_url": "https://ddr.167990.xyz", "fallback_url": "http://100.123.123.17:8787"}
        transport = federation.FedTransport(node)
        calls = []
        transport.client = type("Stub", (), {
            "files": lambda self, path, hidden, limit: calls.append(self.base_url) or {"entries": []},
            "base_url": "https://ddr.167990.xyz",
        })()
        transport.list_files("/srv", False, 100)
        self.assertEqual(calls, ["https://ddr.167990.xyz"])


class TransportWriteTests(unittest.TestCase):
    """父节点这边：路径要还原成对端的编号，调用要落到 /api/fed/... 上。"""

    def node(self):
        return {"id": "a1b2c3d4e5f60718", "prefix": "ddr", "base_url": "http://node", "token": "t"}

    def test_a_prefixed_pane_id_is_stripped_before_talking_to_the_child(self):
        transport = federation.FedTransport(self.node())
        seen = []
        transport.client = type("Stub", (), {
            "input": lambda self, pane, text, submit: seen.append((pane, text, submit)) or {"submitted": True},
            "key": lambda self, pane, key: seen.append((pane, key)) or {"status": "sent"},
        })()
        self.assertTrue(transport.send("a1b2c3d4e5f60718~%12", "hi", True))
        transport.send_key("a1b2c3d4e5f60718~%12", "Escape")
        self.assertEqual(seen, [("%12", "hi", True), ("%12", "Escape")])

    def test_file_helpers_route_through_the_child_api(self):
        transport = federation.FedTransport(self.node())
        calls = []
        transport.client = type("Stub", (), {
            "files": lambda self, path, hidden, limit: calls.append(("list", path)) or {"entries": []},
            "stat": lambda self, path: {"size": 42},
            "download": lambda self, path: b"bytes",
            "upload": lambda self, directory, name, data, offset=0, total=0: calls.append(("put", directory, name)) or {"size": len(data)},
            "rename": lambda self, path, name: {"path": path},
            "delete": lambda self, path, recursive=False: {"path": path},
        })()
        transport.list_files("/srv", False, 100)
        self.assertEqual(transport.file_size("/srv/a"), 42)
        self.assertEqual(transport.download_file("/srv/a", 1000), b"bytes")
        transport.upload_file("/srv/a.txt", b"abc")
        self.assertEqual(calls, [("list", "/srv"), ("put", "/srv", "a.txt")])


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

        # 解析走 hosts 快照 + 共享的 pane 缓存，所以这一步只剩 capture 自己的一次请求
        self.assertEqual(len(after_resolve), 1, f"解析阶段不该再列一次 pane：{after_resolve}")
        self.assertIn("%2512/capture", after_resolve[0])
        self.assertTrue(node_id)

    def test_adding_a_node_from_name_and_auth_key_probes_it(self):
        """界面上只填名字 + auth key：key 里的地址要能被用上，probe 给个明确结论。"""
        added = self.client.post("/api/fed/nodes", json={
            "label": "oracle", "token": "fed_a.b@http://node.invalid"})
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(added.json()["prefix"], "oracle")
        self.assertEqual(added.json()["base_url"], "http://node.invalid")

        def fake_get(client, path, timeout=None):
            if path == "/api/fed/ping":
                return {"tmuxweb": True, "host": "ddr"}
            if path.startswith("/api/fed/panes"):
                return {"panes": [{"id": "%12", "number": 12}]}
            raise AssertionError(path)

        original = federation.FedClient._get
        federation.FedClient._get = fake_get
        try:
            probed = self.client.post(f"/api/fed/nodes/{added.json()['id']}/probe")
        finally:
            federation.FedClient._get = original
        self.assertEqual(probed.status_code, 200, probed.text)
        self.assertEqual(probed.json()["panes"], 1)
        self.assertEqual(probed.json()["host"], "ddr")

    def test_a_public_only_key_is_rewritten_to_the_direct_address_when_the_node_offers_one(self):
        """key 里只有 CF 域名时，问一句对方的内网地址：能直连就别绕 Cloudflare。"""
        def fake_get(client, path, timeout=None):
            if path == "/api/fed/ping":
                return {"tmuxweb": True, "host": "ddr",
                        "addresses": [{"url": "http://10.0.0.9:8787", "kind": "lan"},
                                      {"url": "http://100.123.123.17:8787", "kind": "tailnet"}]}
            raise AssertionError(path)

        original = federation.FedClient._get
        federation.FedClient._get = fake_get
        try:
            added = self.client.post("/api/fed/nodes", json={
                "label": "ddr", "token": "fed_a.b@https://ddr.167990.xyz"})
        finally:
            federation.FedClient._get = original
        self.assertEqual(added.status_code, 201, added.text)
        # tailnet 优先：云主机的 NAT 内网地址往往连不通，挑错它每次都要先等超时
        self.assertEqual(added.json()["base_url"], "http://100.123.123.17:8787")
        self.assertEqual(added.json()["fallback_url"], "https://ddr.167990.xyz")

    def test_the_route_preference_can_be_flipped_from_the_panel(self):
        added = self.client.post("/api/fed/nodes", json={
            "label": "ddr", "token": "fed_a.b@https://ddr.167990.xyz",
            "base_url": "http://100.123.123.17:8787"}).json()
        flipped = self.client.post(f"/api/fed/nodes/{added['id']}/prefer", json={"prefer": "public"})
        self.assertEqual(flipped.status_code, 200, flipped.text)
        self.assertEqual(flipped.json()["base_url"], "https://ddr.167990.xyz")
        self.assertEqual(flipped.json()["fallback_url"], "http://100.123.123.17:8787")

    def test_asking_for_cloudflare_first_keeps_the_public_address(self):
        """选了 CF 优先：base 保持公网地址，直连只作为备用（大文件传输会用到）。"""
        def fake_get(client, path, timeout=None):
            if path == "/api/fed/ping":
                return {"tmuxweb": True, "host": "ddr", "addresses": []}
            raise AssertionError(path)

        original = federation.FedClient._get
        federation.FedClient._get = fake_get
        try:
            added = self.client.post("/api/fed/nodes", json={
                "label": "ddr", "token": "fed_a.b@https://ddr.167990.xyz", "prefer": "public"})
        finally:
            federation.FedClient._get = original
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(added.json()["base_url"], "https://ddr.167990.xyz")
        self.assertEqual(added.json()["prefer"], "public")

    def test_choosing_cloudflare_still_records_the_direct_address_as_a_spare(self):
        def fake_get(client, path, timeout=None):
            if path == "/api/fed/ping":
                return {"tmuxweb": True, "host": "ddr",
                        "addresses": [{"url": "http://100.123.123.17:8787", "kind": "tailnet"}]}
            raise AssertionError(path)

        original = federation.FedClient._get
        federation.FedClient._get = fake_get
        try:
            added = self.client.post("/api/fed/nodes", json={
                "label": "ddr", "token": "fed_a.b@https://ddr.167990.xyz", "prefer": "public"})
        finally:
            federation.FedClient._get = original
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(added.json()["base_url"], "https://ddr.167990.xyz")
        self.assertEqual(added.json()["fallback_url"], "http://100.123.123.17:8787")

    def test_the_two_addresses_can_be_replaced_by_hand(self):
        added = self.client.post("/api/fed/nodes", json={
            "label": "old", "token": "fed_a.b@https://ddr.167990.xyz"}).json()
        saved = self.client.post(f"/api/fed/nodes/{added['id']}/addresses", json={
            "base_url": "http://100.123.123.17:8787/", "fallback_url": " https://ddr.167990.xyz "})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["base_url"], "http://100.123.123.17:8787")
        self.assertEqual(saved.json()["fallback_url"], "https://ddr.167990.xyz")
        # 只给一个地址也行，顺序以 base 为准
        only = self.client.post(f"/api/fed/nodes/{added['id']}/addresses",
                                json={"base_url": "", "fallback_url": "https://ddr.167990.xyz"})
        self.assertEqual(only.json()["base_url"], "https://ddr.167990.xyz")
        self.assertEqual(only.json()["fallback_url"], "")
        self.assertEqual(self.client.post(f"/api/fed/nodes/{added['id']}/addresses",
                                          json={"base_url": "ftp://x"}).status_code, 422)

    def test_a_probe_backfills_the_direct_address_on_an_old_node(self):
        # 模拟一条"只记了 CF 地址"的旧节点：直接写库，不走 add 的探测
        node_id = self.store.add("ddr", "ddr", "https://ddr.167990.xyz", "", "fed_a.b")["id"]

        def fake_get(client, path, timeout=None):
            if path == "/api/fed/ping":
                return {"tmuxweb": True, "host": "ddr",
                        "addresses": [{"url": "http://100.123.123.17:8787", "kind": "tailnet"}]}
            if path.startswith("/api/fed/panes"):
                return {"panes": []}
            raise AssertionError(path)

        original = federation.FedClient._get
        federation.FedClient._get = fake_get
        try:
            probed = self.client.post(f"/api/fed/nodes/{node_id}/probe")
        finally:
            federation.FedClient._get = original
        self.assertEqual(probed.status_code, 200, probed.text)
        self.assertEqual(probed.json()["learned"], "http://100.123.123.17:8787")
        stored = self.client.get("/api/fed/nodes").json()["nodes"][0]
        # 学到的直连地址直接排到最前面，域名降成备用 —— 不用再绕 CF
        self.assertEqual(stored["base_url"], "http://100.123.123.17:8787")
        self.assertEqual(stored["fallback_url"], "https://ddr.167990.xyz")
        self.assertEqual([entry["kind"] for entry in stored["addresses"]], ["tailnet", "public"])

    def test_a_typed_address_wins_and_the_one_in_the_key_becomes_the_spare(self):
        added = self.client.post("/api/fed/nodes", json={
            "label": "oracle", "token": "fed_a.b@https://ddr.167990.xyz",
            "base_url": "http://100.123.123.17:8787"})
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual(added.json()["base_url"], "http://100.123.123.17:8787")
        self.assertEqual(added.json()["fallback_url"], "https://ddr.167990.xyz")

    def test_a_node_without_an_auth_key_is_refused(self):
        refused = self.client.post("/api/fed/nodes", json={"label": "oracle"})
        self.assertEqual(refused.status_code, 422)

    def test_an_unknown_prefix_says_so(self):
        missing = self.client.get("/api/panes/nope%2312/capture")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("nope", missing.json()["detail"])


if __name__ == "__main__":
    unittest.main()
