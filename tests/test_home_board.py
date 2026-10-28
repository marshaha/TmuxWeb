import json
import unittest
from urllib.parse import quote

from fastapi.testclient import TestClient

from tmux_web import create_app
from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTmux
from tests.fake_terminal import FakeTerminalFactory

PASSWORD = "home-test-password"


class HomeBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmux = FakeTmux()
        self.client = TestClient(create_app(
            self.tmux,
            password=PASSWORD,
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:",
            home_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))
        self.client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    def favourite(self, pane="%4"):
        return self.client.post("/api/home/favorites", json={"pane": pane})

    # ------------------------------------------------------------------ pages
    def test_the_home_page_is_served_and_requires_a_login(self):
        anonymous = TestClient(create_app(
            FakeTmux(), password=PASSWORD, session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:", home_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        ))
        # the page itself is a static shell: the board behind it needs a session
        self.assertEqual(anonymous.get("/home").status_code, 200)
        self.assertEqual(anonymous.get("/api/home").status_code, 401)

        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        for marker in ("收藏", "看板卡片", "/api/home/search", "publish_home_card"):
            self.assertIn(marker, response.text)

    def test_the_console_offers_a_star_per_pane_and_a_home_link(self):
        page = self.client.get("/").text
        self.assertIn('data-star=', page)
        self.assertIn('href="/home"', page)
        self.assertIn('id="i-star-fill"', page)
        self.assertIn("toggleFavorite", page)
        # the star must not be reported as a pane target
        self.assertNotIn('data-pane="%4" data-star', page)

    # ------------------------------------------------------------------ favorites
    def test_a_favourite_shows_up_on_the_board_with_status_and_preview(self):
        self.tmux.panes[0]["command"] = "claude"   # the fake only has a transcript for claude
        self.assertEqual(self.favourite().status_code, 201)

        board = self.client.get("/api/home").json()
        self.assertEqual([item["pane"] for item in board["favorites"]], ["%4"])
        entry = board["favorites"][0]
        self.assertEqual(entry["provider"], "claude")
        self.assertEqual(entry["title"], "Review | repo")
        self.assertEqual(entry["path"], "/work/repo")
        self.assertIn(entry["status"], {"idle", "busy", "unknown"})
        self.assertEqual(entry["preview"]["role"], "assistant")
        self.assertIn("A", entry["preview"]["text"])
        self.assertEqual(board["stats"]["favorites"], 1)

    def test_favourites_are_listed_removed_and_validated(self):
        self.assertEqual(self.client.get("/api/home/favorites").json()["favorites"], [])
        self.assertEqual(self.favourite().status_code, 201)
        self.assertEqual(self.client.get("/api/home/favorites").json()["favorites"], ["%4"])

        removed = self.client.delete("/api/home/favorites/" + quote("%4", safe=""))
        self.assertEqual(removed.status_code, 200)
        self.assertTrue(removed.json()["removed"])
        self.assertEqual(self.client.get("/api/home/favorites").json()["favorites"], [])

        self.assertEqual(self.favourite("%99").status_code, 404)

    # ------------------------------------------------------------------ search
    def test_search_matches_pane_names_and_message_text(self):
        self.tmux.panes[0]["command"] = "claude"
        by_title = self.client.get("/api/home/search", params={"q": "Review"}).json()
        self.assertEqual([pane["id"] for pane in by_title["panes"]], ["%4"])

        by_message = self.client.get("/api/home/search", params={"q": "问题"}).json()
        self.assertEqual(len(by_message["messages"]), 1)
        hit = by_message["messages"][0]
        self.assertEqual(hit["pane"], "%4")
        self.assertEqual(hit["role"], "user")
        self.assertIn("问题", hit["snippet"])

        self.assertEqual(self.client.get("/api/home/search", params={"q": "  "}).json()["messages"], [])

    # ------------------------------------------------------------------ cards
    def test_cards_can_be_published_read_and_removed(self):
        created = self.client.post("/api/home/cards", json={
            "title": "构建完成", "body": "apk 已出包", "pane": "%4", "kind": "result",
        })
        self.assertEqual(created.status_code, 201)
        card = created.json()
        self.assertEqual(card["kind"], "result")

        board = self.client.get("/api/home").json()
        self.assertEqual([item["title"] for item in board["cards"]], ["构建完成"])
        self.assertEqual(board["stats"]["cards"], 1)

        found = self.client.get("/api/home/search", params={"q": "apk"}).json()["cards"]
        self.assertEqual(found[0]["id"], card["id"])

        self.assertEqual(self.client.delete(f"/api/home/cards/{card['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/home/cards/{card['id']}").status_code, 404)
        self.assertEqual(self.client.post("/api/home/cards", json={"title": ""}).status_code, 422)

    def test_mcp_publishes_a_card_and_lists_the_board(self):
        tools = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        names = [tool["name"] for tool in tools["result"]["tools"]]
        for expected in ("publish_home_card", "list_home", "remove_home_card"):
            self.assertIn(expected, names)

        published = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "publish_home_card",
                       "arguments": {"title": "来自 agent 的结论", "body": "等 review", "kind": "result"}},
        }).json()
        payload = json.loads(published["result"]["content"][0]["text"])
        self.assertEqual(payload["card"]["title"], "来自 agent 的结论")
        self.assertEqual(payload["visible_at"], "/home")

        board = json.loads(self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_home", "arguments": {"limit": 5}},
        }).json()["result"]["content"][0]["text"])
        self.assertEqual([card["title"] for card in board["cards"]], ["来自 agent 的结论"])

        card_id = board["cards"][0]["id"]
        removed = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "remove_home_card", "arguments": {"id": card_id}},
        }).json()
        self.assertTrue(json.loads(removed["result"]["content"][0]["text"])["removed"])

        missing = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "publish_home_card", "arguments": {}},
        }).json()
        self.assertTrue(missing["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
