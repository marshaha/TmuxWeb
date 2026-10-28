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
        # the board keeps a collapsible sidebar with the same tree as the console
        for marker in ('id="tree"', 'id="side-collapse"', 'id="side-open"', 'data-star=',
                       'data-pane=', "tmux-home-sidebar", "loadTree",
                       'data-rename=', 'id="rename-dialog"', "openRename",
                       'id="hover-preview"', 'agent_preview', "showHover"):
            self.assertIn(marker, response.text)
        # cards live in a responsive grid: main must stretch inside the flex column,
        # otherwise `margin:0 auto` shrinks it and only one card fits per row
        self.assertIn("main{width:100%;padding:16px;max-width:1240px;margin:0 auto}", response.text)
        self.assertIn("minmax(min(268px,100%),1fr)", response.text)
        # the sidebar already links to the console, so the header keeps no duplicate
        self.assertNotIn('title="回到控制台"', response.text)

    def test_the_sidebar_width_is_draggable_and_remembered(self):
        home = self.client.get("/home")
        for marker in ('id="side-resizer"', "--side-w", "tmux-home-side-w", "col-resize"):
            self.assertIn(marker, home.text)
        # the phone drawer keeps a fixed width: a wide desktop drag must not
        # leave a phone with a sidebar that covers the whole screen
        self.assertIn("width:min(86vw,320px)", home.text)

        console = self.client.get("/")
        for marker in ('id="side-resizer"', "var(--side-w,280px)", "tmux-side-w"):
            self.assertIn(marker, console.text)
        # collapsing must still win over the stored width
        self.assertIn(".app.sidebar-collapsed{grid-template-columns:0 minmax(0,1fr)}", console.text)

        # a narrow sidebar must not lose its action buttons: the row wraps
        # instead of pushing the last icon under the clipped edge
        self.assertIn('<div class="side-actions">', home.text)
        for page in (home.text, console.text):
            self.assertIn("flex-wrap:wrap;row-gap:", page)
            self.assertIn("initIconRowScroll", page)

    def test_the_full_preview_page_is_served_and_wired_to_the_board(self):
        preview = self.client.get("/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["cache-control"], "no-store")
        for marker in ("/api/panes/", "/interrupt", "Enter 发送", 'id="back"', "marked.esm.js"):
            self.assertIn(marker, preview.text)

        page = self.client.get("/home").text
        for marker in ('data-full=', 'id="preview-drawer"', 'id="pd-frame"', "PREVIEW_WIDE",
                       "openFullPreview", "closeFullPreview"):
            self.assertIn(marker, page)
        # the hover panel has to accept the pointer, otherwise it cannot be scrolled
        self.assertIn("pointer-events:auto;overscroll-behavior:contain", page)

    def test_the_console_offers_a_star_per_pane_and_a_home_link(self):
        page = self.client.get("/").text
        self.assertIn('data-star=', page)
        self.assertIn('href="/home"', page)
        self.assertIn('id="i-star-fill"', page)
        self.assertIn("toggleFavorite", page)
        # the star must not be reported as a pane target
        self.assertNotIn('data-pane="%4" data-star', page)

    def test_an_open_conversation_has_a_star_in_its_top_bar(self):
        console = self.client.get("/").text
        for marker in ('id="fav-toggle"', "syncFavButton", "#i-star-fill",
                       "$('fav-toggle').onclick"):
            self.assertIn(marker, console)
        # the star follows the selection, so it must be refreshed by selectPane too
        self.assertIn("syncLiveButton();syncFavButton();", console)

        preview = self.client.get("/preview", params={"pane": "%7"}).text
        for marker in ('id="fav"', "function syncFav", "/api/home/favorites", "paintFav"):
            self.assertIn(marker, preview)

    # ------------------------------------------------------------------ favorites
    def test_a_favourite_shows_up_on_the_board_with_status_and_preview(self):
        self.tmux.panes[0]["command"] = "claude"   # the fake only has a transcript for claude
        self.assertEqual(self.favourite().status_code, 201)

        board = self.client.get("/api/home").json()
        self.assertEqual([item["pane"] for item in board["favorites"]], ["%4"])
        entry = board["favorites"][0]
        self.assertEqual(entry["provider"], "claude")
        # the default card name is session · window · host, not the raw pane title
        self.assertEqual(entry["default_title"], "codex · 0: editor · localhost")
        self.assertEqual(entry["title"], "codex · 0: editor · localhost")
        self.assertFalse(entry["custom_title"])
        self.assertEqual(entry["pane_title"], "Review | repo")
        self.assertEqual(entry["host"], "localhost")
        self.assertEqual(entry["path"], "/work/repo")
        self.assertIn(entry["status"], {"idle", "busy", "unknown"})
        self.assertEqual(entry["preview"]["role"], "assistant")
        self.assertIn("A", entry["preview"]["text"])
        self.assertEqual(board["stats"]["favorites"], 1)

    def test_several_favourites_all_come_back(self):
        self.tmux.panes.append({
            "id": "%12", "target": "shell:0.0", "session": "shell", "window": "0",
            "window_name": "shell", "index": "0", "command": "zsh", "title": "SY0100667",
            "path": "/tmp", "active": False,
        })
        for pane in ("%4", "%12"):
            self.assertEqual(self.favourite(pane).status_code, 201)
        board = self.client.get("/api/home").json()
        self.assertEqual(sorted(item["pane"] for item in board["favorites"]), ["%12", "%4"])
        self.assertEqual(board["stats"]["favorites"], 2)
        # a shell pane has no transcript, so its card falls back to the pane title
        shell = next(item for item in board["favorites"] if item["pane"] == "%12")
        self.assertIsNone(shell["preview"])
        self.assertEqual(shell["default_title"], "shell · 0: shell · localhost")

    def test_favourites_are_listed_removed_and_validated(self):
        self.assertEqual(self.client.get("/api/home/favorites").json()["favorites"], [])
        self.assertEqual(self.favourite().status_code, 201)
        self.assertEqual(self.client.get("/api/home/favorites").json()["favorites"], ["%4"])

        removed = self.client.delete("/api/home/favorites/" + quote("%4", safe=""))
        self.assertEqual(removed.status_code, 200)
        self.assertTrue(removed.json()["removed"])
        self.assertEqual(self.client.get("/api/home/favorites").json()["favorites"], [])

        # a pane that is gone (or whose host is unreachable) can still be bookmarked
        self.assertEqual(self.favourite("%99").status_code, 201)
        offline = next(item for item in self.client.get("/api/home").json()["favorites"] if item["pane"] == "%99")
        self.assertEqual(offline["status"], "offline")
        self.assertIsNone(offline["preview"])
        self.client.delete("/api/home/favorites/" + quote("%99", safe=""))

        # a malformed reference is still rejected
        self.assertEqual(self.favourite("nonsense").status_code, 422)

    def test_the_hover_preview_skips_a_trailing_user_turn(self):
        self.tmux.panes[0]["command"] = "claude"
        self.tmux.transcript = lambda pane_id, limit=200: {
            "pane": pane_id, "session_id": "s", "mtime": 1,
            "messages": [
                {"id": "u1", "role": "user", "text": "先看一下", "timestamp": None},
                {"id": "a1", "role": "assistant", "text": "第一版结论：a=1", "timestamp": None},
                {"id": "u2", "role": "user", "text": "继续，跑第二版", "timestamp": None},
            ],
        }
        self.assertEqual(self.favourite().status_code, 201)

        entry = self.client.get("/api/home").json()["favorites"][0]
        # the card body still shows the newest message
        self.assertEqual(entry["preview"]["role"], "user")
        self.assertIn("继续", entry["preview"]["text"])
        # the hover card shows the agent's last real answer instead
        self.assertEqual(entry["agent_preview"]["role"], "assistant")
        self.assertEqual(entry["agent_preview"]["text"], "第一版结论：a=1")
        self.assertFalse(entry["agent_preview"]["truncated"])

    def test_the_hover_preview_is_capped(self):
        self.tmux.panes[0]["command"] = "claude"
        self.tmux.transcript = lambda pane_id, limit=200: {
            "pane": pane_id, "session_id": "s", "mtime": 1,
            "messages": [{"id": "a1", "role": "assistant", "text": "x" * 9000, "timestamp": None}],
        }
        self.favourite()
        entry = self.client.get("/api/home").json()["favorites"][0]
        self.assertEqual(len(entry["agent_preview"]["text"]), 6000)
        self.assertTrue(entry["agent_preview"]["truncated"])

    def test_a_card_can_be_renamed_and_reset_to_the_default(self):
        self.tmux.panes[0]["command"] = "claude"
        self.assertEqual(self.favourite().status_code, 201)

        renamed = self.client.patch("/api/home/favorites/" + quote("%4", safe=""),
                                    json={"title": "ETH 收敛 · 等 review"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "ETH 收敛 · 等 review")

        entry = self.client.get("/api/home").json()["favorites"][0]
        self.assertEqual(entry["title"], "ETH 收敛 · 等 review")
        self.assertTrue(entry["custom_title"])
        self.assertEqual(entry["default_title"], "codex · 0: editor · localhost")

        # an empty title restores the generated name
        reset = self.client.patch("/api/home/favorites/" + quote("%4", safe=""), json={"title": ""})
        self.assertEqual(reset.status_code, 200)
        entry = self.client.get("/api/home").json()["favorites"][0]
        self.assertEqual(entry["title"], "codex · 0: editor · localhost")
        self.assertFalse(entry["custom_title"])

        missing = self.client.patch("/api/home/favorites/" + quote("%99", safe=""), json={"title": "x"})
        self.assertEqual(missing.status_code, 404)

    def test_a_favourite_can_be_saved_with_its_own_name(self):
        self.tmux.panes[0]["command"] = "claude"
        created = self.client.post("/api/home/favorites", json={"pane": "%4", "title": "我的任务"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["title"], "我的任务")
        entry = self.client.get("/api/home").json()["favorites"][0]
        self.assertEqual(entry["title"], "我的任务")
        self.assertTrue(entry["custom_title"])

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

    def test_search_puts_servers_and_sessions_before_messages(self):
        self.tmux.panes[0]["command"] = "claude"
        self.tmux.panes[0]["session"] = "codex-work"
        self.tmux.panes[0]["target"] = "codex-work:0.0"
        result = self.client.get("/api/home/search", params={"q": "codex"}).json()
        self.assertEqual(result["mode"], "literal")
        self.assertTrue(result["sessions"], "the tmux session name must be in its own group")
        self.assertEqual(result["sessions"][0]["session"], "codex-work")
        self.assertEqual(result["panes"][0]["match"], "name")
        # a plain word stays literal: no regex surprises like "a.b" matching "axb"
        literal = self.client.get("/api/home/search", params={"q": "codex-work:0.0"}).json()
        self.assertEqual(literal["mode"], "regex")
        dotted = self.client.get("/api/home/search", params={"q": "a.b"}).json()
        self.assertEqual(dotted["mode"], "regex")  # a dot is a metacharacter
        self.assertEqual(self.client.get("/api/home/search", params={"q": "cod"}).json()["mode"], "literal")

    def test_search_supports_regular_expressions(self):
        self.tmux.panes[0]["command"] = "claude"
        anchored = self.client.get("/api/home/search", params={"q": "^Review"}).json()
        self.assertEqual(anchored["mode"], "regex")
        self.assertEqual([pane["id"] for pane in anchored["panes"]], ["%4"])

        alternation = self.client.get("/api/home/search", params={"q": "nomatch|Review"}).json()
        self.assertEqual([pane["id"] for pane in alternation["panes"]], ["%4"])

        # a broken pattern must not fail the request: it falls back to literal
        broken = self.client.get("/api/home/search", params={"q": "Review("}).json()
        self.assertEqual(broken["mode"], "literal-fallback")
        self.assertEqual(broken["panes"], [])

    def test_search_matches_a_saved_server_name(self):
        self.tmux.panes[0]["command"] = "claude"
        created = self.client.post("/api/remote-hosts", json={
            "name": "build-box", "hostname": "10.0.0.55", "port": 22, "username": "ubuntu",
            "auth_type": "agent", "verify": False,
        })
        self.assertEqual(created.status_code, 201)
        result = self.client.get("/api/home/search", params={"q": "build-box"}).json()
        self.assertEqual([host["name"] for host in result["hosts"]], ["build-box"])
        self.assertEqual(result["hosts"][0]["hostname"], "10.0.0.55")

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
