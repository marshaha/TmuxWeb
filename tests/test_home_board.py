import json
import time
import unittest
from unittest import mock

import remote_hosts
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
        # the board keeps a collapsible sidebar with the same tree as the console;
        # the tree itself now lives in shell.js (shared with /grid), not in the page
        for marker in ('id="tree"', 'id="side-collapse"', 'id="side-open"',
                       'src="/assets/shell.js"', "TmuxShell.mount(",
                       'data-rename=', 'id="rename-dialog"', "openRename",
                       # 预览面板是共享模块（侧栏也用同一份），页面只负责喂数据
                       "TmuxPanePreview.mount(", "panePreview.show(card,paneId,{sticky})",
                       'src="/assets/pane-preview.js"'):
            self.assertIn(marker, response.text)
        shell = self.client.get("/assets/shell.js").text
        for marker in ('data-star=', 'data-pane=', "function loadHosts", "function renderTree"):
            self.assertIn(marker, shell)
        # one storage key for the collapsed sidebar on every page (shell.js owns it)
        self.assertIn("tmux-sidebar-collapsed", self.client.get("/assets/shell.js").text)
        self.assertIn("collapse(on) { syncCollapsed(!!on); },", self.client.get("/assets/shell.js").text)
        # cards live in a responsive grid: main must stretch inside the flex column,
        # otherwise `margin:0 auto` shrinks it and only one card fits per row
        self.assertIn("main{width:100%;padding:16px;max-width:1240px;margin:0 auto}", response.text)
        self.assertIn("minmax(min(268px,100%),1fr)", response.text)
        # the sidebar already links to the console, so the header keeps no duplicate
        self.assertNotIn('title="回到控制台"', response.text)

    def test_the_board_answers_before_a_slow_host_does(self):
        """A favourite on a hung host must not hold the whole board hostage."""
        created = self.client.post("/api/remote-hosts", json={
            "name": "blackhole", "hostname": "192.0.2.1", "port": 22, "username": "nobody",
            "auth_type": "password", "credential": "secret", "verify": False,
        })
        self.assertEqual(created.status_code, 201)
        host_id = created.json()["id"]
        pane = f"{host_id}~%1"
        self.assertEqual(self.client.post("/api/home/favorites", json={"pane": pane}).status_code, 201)

        def hung_list_panes(_self, timeout=15):
            time.sleep(4)
            raise RuntimeError("ssh timed out")

        with mock.patch.object(FakeSSHRunner, "list_panes", hung_list_panes):
            started = time.time()
            board = self.client.get("/api/home").json()
            elapsed = time.time() - started
        # TestClient drains the worker thread before returning, so this only guards
        # against falling back to the 15s SSH timeout; the endpoint itself answers at
        # TMUX_WEB_HOME_BUDGET_SECONDS (measured live: 1.2s with the same hung host).
        self.assertLess(elapsed, 4.5, f"/api/home took {elapsed:.1f}s with a hung host")
        entry = next(item for item in board["favorites"] if item["pane"] == pane)
        # a placeholder is better than blocking every other card
        self.assertTrue(entry.get("pending"))
        self.assertEqual(entry["host"], "blackhole")
        self.client.delete(f"/api/home/favorites/{pane}")
        self.client.delete(f"/api/remote-hosts/{host_id}")

    def test_the_board_is_painted_from_a_local_snapshot(self):
        page = self.client.get("/home").text
        for marker in ("tmux-home-cache-v1", "readCache", "writeCache", "slimBoard",
                       "stale-note", "skel", "显示缓存"):
            self.assertIn(marker, page)

    def test_recent_visits_show_up_on_the_board(self):
        resp = self.client.post("/api/home/recent", json={"pane": "%4"})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.client.post("/api/home/recent", json={"pane": "nonsense"}).status_code, 422)
        board = self.client.get("/api/home").json()
        self.assertIn("recent", board)
        self.assertEqual([item["pane"] for item in board["recent"]], ["%4"])
        entry = board["recent"][0]
        self.assertEqual(entry["pane"], "%4")
        self.assertTrue(entry["title"])          # title/session come from the host snapshot
        page = self.client.get("/home").text
        for marker in ("最近访问", "recentCard", 'id="recent"', 'id="recent-count"'):
            self.assertIn(marker, page)

    def test_recent_visits_have_their_own_lightweight_endpoint(self):
        """The sidebar's 最近 block must not pay for a full /api/home sweep."""
        for pane in ("%4", "%5", "%4"):
            self.client.post("/api/home/recent", json={"pane": pane})
        data = self.client.get("/api/home/recent?limit=5").json()
        panes = [row["pane"] for row in data["recent"]]
        self.assertEqual(panes[0], "%4")          # 最近的一次排最前，且不重复
        self.assertEqual(sorted(panes), ["%4", "%5"])
        self.assertTrue(all(row["visited_at"] > 0 for row in data["recent"]))
        self.assertEqual(len(self.client.get("/api/home/recent?limit=1").json()["recent"]), 1)
        # 删除一条就不再出现
        self.client.delete("/api/home/recent/%254")
        self.assertNotIn("%4", [row["pane"] for row in self.client.get("/api/home/recent").json()["recent"]])

    def test_the_console_sidebar_lists_recent_chats_under_the_tree(self):
        page = self.client.get("/").text
        # 标记和逻辑都在共用模块里（三个页面同一份），控制台只负责挂载
        self.assertIn("/assets/recents.js", page)
        self.assertIn("/assets/recents.css", page)
        self.assertIn("TmuxRecents.mount(", page)
        self.assertIn("hideWhenFiles: true", page)
        self.assertNotIn('id="recent-block"', page)      # 页面里不再抄一份标记
        recent = self.recent_script()
        self.assertIn("section.id = 'recent-block'", recent)
        self.assertIn('id="recent-list"', recent)
        self.assertIn("data-recent=", recent)
        self.assertIn("/api/home/recent?limit=", recent)
        # 文件面板打开时这块让位（控制台的行内实现也是这个规则）
        self.assertIn("filesOpen()", recent)
        self.assertIn("attributeFilter: ['hidden']", recent)

    def recent_script(self):
        return self.client.get("/assets/recents.js").text

    def recent_styles(self):
        return self.client.get("/assets/recents.css").text

    def test_the_recent_block_is_one_implementation_for_all_three_pages(self):
        """控制台 / 主页 / 多窗口 用同一份最近块，页面只决定点一行做什么。"""
        for path in ("/", "/home", "/grid"):
            page = self.client.get(path).text
            self.assertIn("/assets/recents.js", page, path)
            self.assertIn("/assets/recents.css", page, path)
            self.assertNotIn('id="recent-block"', page, path)   # 谁都不许再抄一份
        shell = self.client.get("/assets/shell.js").text
        self.assertIn("TmuxRecents.mount(", shell)
        self.assertIn("TmuxRecents.touch(pane)", shell)          # 点一行先记一次访问
        self.assertIn("if (state.hooks.onPane) state.hooks.onPane(pane)", shell)
        self.assertIn("TmuxRecents.setHosts(state.hosts)", shell)
        self.assertIn("TmuxRecents.setStates(state.states)", shell)
        self.assertIn("TmuxRecents.setFavorites", shell)
        self.assertIn("tmuxweb:favorites", shell)
        # 三个页面的点击行为不同：控制台切会话、主页跳控制台、多窗口加窗口
        self.assertIn("onPick: pane => { selectPane(pane);", self.client.get("/").text)
        self.assertIn("onPane:paneId=>{location=consoleUrl(paneId);}", self.client.get("/home").text)
        self.assertIn("onPane: paneId => openPane(paneId)", self.client.get("/grid").text)

    def test_pinned_panes_sit_on_top_of_the_recent_block(self):
        """钉住 = 跟账号走的置顶，掉出最近 30 条也还在；普通最近里不再重复出现。"""
        for pane in ("%4", "%5"):
            self.client.post("/api/home/recent", json={"pane": pane})
        resp = self.client.post("/api/home/recent/pin", json={"pane": "%4", "label": "localhost · codex"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["pinned"])
        data = self.client.get("/api/home/recent?limit=8").json()
        self.assertEqual(data["pinned"], ["%4"])
        panes = [row["pane"] for row in data["recent"]]
        self.assertEqual(panes, ["%4", "%5"])       # 固定的排最前，且不重复
        self.assertTrue(data["recent"][0]["pinned"])
        self.assertEqual(data["recent"][0]["label"], "localhost · codex")
        self.assertFalse(data["recent"][1]["pinned"])
        # 收藏状态跟着一起来，侧栏那一行不用再单独请求
        self.favourite("%5")
        data = self.client.get("/api/home/recent?limit=8").json()
        self.assertEqual({row["pane"]: row["favorite"] for row in data["recent"]}, {"%4": False, "%5": True})
        # 取消固定
        self.assertTrue(self.client.post("/api/home/recent/pin", json={"pane": "%4", "pinned": False}).json()["removed"])
        data = self.client.get("/api/home/recent?limit=8").json()
        self.assertEqual(data["pinned"], [])
        self.assertFalse(any(row["pinned"] for row in data["recent"]))
        # 垃圾 pane 引用 / 未登录
        self.assertEqual(self.client.post("/api/home/recent/pin", json={"pane": "nonsense"}).status_code, 422)
        self.assertEqual(self.client.post("/api/home/recent/pin", json={"pane": ""}).status_code, 422)

    def test_a_pin_survives_the_recent_cap_and_a_missing_host(self):
        """固定的项不该被最近 30 条挤掉，主机掉线时也还能看见它当初的标题。"""
        store = __import__("home_store").HomeStore(":memory:")
        for index in range(40):
            store.touch_recent(f"%{index}")
        store.pin("%0", "oracle · claude")
        self.assertIn("%0", [row["pane_id"] for row in store.pins()])
        self.assertTrue(store.is_pinned("%0"))
        store.pin("%0", "oracle · claude: main")     # 再钉一次只更新标签
        self.assertEqual(store.pins()[0]["label"], "oracle · claude: main")
        self.assertEqual(len(store.pins()), 1)
        self.assertTrue(store.unpin("%0"))
        self.assertFalse(store.unpin("%0"))          # 取消两次不报错
        store.close()

    def test_a_pinned_pane_can_be_renamed_and_the_card_follows(self):
        """一个 pane 只有一个名字：侧栏改名要同步到收藏卡片，反过来也一样。"""
        self.client.post("/api/home/recent", json={"pane": "%4"})
        self.client.post("/api/home/recent/pin", json={"pane": "%4", "label": "localhost · codex"})
        self.favourite("%4")
        resp = self.client.patch("/api/home/recent/pin/%254", json={"label": "Codex 主力"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["label"], "Codex 主力")
        data = self.client.get("/api/home/recent?limit=8").json()
        self.assertEqual(data["recent"][0]["label"], "Codex 主力")
        self.assertEqual(data["recent"][0]["title"], "Codex 主力")      # 收藏卡片的标题
        board = self.client.get("/api/home").json()
        self.assertEqual([f.get("title") for f in board["favorites"] if f.get("pane") == "%4"], ["Codex 主力"])
        # 在看板上改名，钉住的那条跟着改
        self.assertEqual(self.client.patch("/api/home/favorites/%254", json={"title": "看板改的"}).status_code, 200)
        data = self.client.get("/api/home/recent?limit=8").json()
        self.assertEqual(data["recent"][0]["label"], "看板改的")
        # 清空名字 = 回到自动生成的那个
        self.assertEqual(self.client.patch("/api/home/recent/pin/%254", json={"label": ""}).status_code, 200)
        self.assertEqual(self.client.get("/api/home/recent").json()["recent"][0]["label"], "")
        # 没钉住的不给改名；空白 pane 直接拒
        self.assertEqual(self.client.patch("/api/home/recent/pin/%255", json={"label": "x"}).status_code, 404)
        self.assertEqual(self.client.patch("/api/home/recent/pin/%20", json={"label": "x"}).status_code, 422)

    def test_pinning_never_copies_the_card_title(self):
        """钉住和收藏是两份独立的数据：钉的时候不从收藏卡片借名字。"""
        self.favourite("%6")
        self.client.patch("/api/home/favorites/%256", json={"title": "HERMES 板子"})
        self.client.post("/api/home/recent/pin", json={"pane": "%6", "label": "localhost · hermes"})
        data = self.client.get("/api/home/recent?limit=8").json()
        self.assertEqual(data["recent"][0]["label"], "localhost · hermes")
        self.assertEqual(data["recent"][0]["title"], "HERMES 板子")     # 各是各的
        # 收藏本身也绝不会凭空长出一条钉子
        store = __import__("home_store").HomeStore(":memory:")
        store.add_favorite("%7", title="x")
        self.assertEqual(store.pins(), [])
        store.close()

    def test_a_pinned_row_shows_the_favorite_name_when_it_has_no_label_of_its_own(self):
        """钉子区 #6 这种：钉住但没单独改名 → 侧栏要显示收藏处的名字，不要退化成 claude · 0: main · oracle。"""
        self.favourite("%6")
        self.client.patch("/api/home/favorites/%256", json={"title": "DDR分析"})
        self.client.post("/api/home/recent/pin", json={"pane": "%6", "label": ""})
        row = next(r for r in self.client.get("/api/home/recent?limit=8").json()["recent"]
                   if r["pane"] == "%6")
        self.assertEqual(row["label"], "")          # 钉子自己没有名字
        self.assertEqual(row["title"], "DDR分析")    # 收藏处有
        self.assertTrue(row["pinned"])
        # 渲染时的优先级：钉子名字 → 收藏名字 → 自动名
        script = self.client.get("/assets/recents.js").text
        self.assertIn("const favorite_name = (entry.title || '').trim();", script)
        self.assertIn("const label = custom || favorite_name || fallback;", script)
        # 一个 pane 的“人起的名字”只此一个出口，顶栏也用它
        self.assertIn("function nameFor(id) {", script)
        self.assertIn("return (entry.label || '').trim() || (entry.title || '').trim();", script)
        self.assertIn("nameFor,", script)
        page = self.client.get("/?pane=%2528").text
        self.assertIn("TmuxRecents.nameFor(id)", page)
        self.assertIn("const shownTitle=customName||p.title||p.target;", page)

    def test_renaming_a_pin_needs_it_to_be_pinned(self):
        store = __import__("home_store").HomeStore(":memory:")
        with self.assertRaises(KeyError):
            store.rename_pin("%7", "x")
        store.pin("%7", "a")
        self.assertEqual(store.rename_pin("%7", "b")["label"], "b")
        with self.assertRaises(ValueError):
            store.rename_pin("", "b")
        store.close()

    def test_the_sidebar_renames_a_pinned_row_in_place(self):
        page = self.recent_script()
        for marker in ('data-rename="', "#i-pencil", "recent-rename", "function startRename(pane)",
                       "startRename(row.dataset.row)", "PATCH", "/api/home/recent/pin/", "双击改名"):
            self.assertIn(marker, page)
        # 双击 / 触屏长按都能进编辑，Esc 放弃
        self.assertIn("addEventListener('dblclick'", page)
        self.assertIn("480", page)
        # 只更新高亮时不重建行，但正在改名时必须重建（否则输入框被留着或值被冲掉）
        self.assertIn("&& state.list.children.length && !state.renaming", page)
        self.assertIn("state.signature = '';", page)

    def test_the_recent_rows_carry_a_pin_and_a_star(self):
        page = self.recent_script()
        for marker in ('data-pin="', 'data-star="', "async function togglePin(pane)", "#i-pin", "#i-pin-fill",
                       "recent-act", "touch: noteVisit"):
            self.assertIn(marker, page)
        # 固定区自成一块浅底 + 列表可滚（不够显示就滚/滑）
        styles = self.recent_styles()
        self.assertIn(".recent-pins{", styles)
        self.assertIn('class="recent-pins"', page)
        self.assertIn("color-mix(in srgb,var(--green) 16%,transparent)", styles)   # 固定区是淡绿（跟主题走）
        self.assertIn("padding:8px calc(4px + env(safe-area-inset-right)) calc(6px + env(safe-area-inset-bottom)) calc(8px + env(safe-area-inset-left))", styles)
        self.assertNotIn("color-mix(in srgb,var(--accent) 9%,transparent)", styles)
        self.assertIn("overscroll-behavior:contain", styles)
        self.assertIn("-webkit-overflow-scrolling:touch", styles)
        # 清空只清普通最近，固定的不动
        self.assertIn("state.entries.filter(entry => !entry.pinned)", page)

    def test_the_home_sidebar_keeps_one_chat_entry(self):
        """home 侧栏只留一个"去聊天"的入口，图标用聊天气泡。"""
        page = self.client.get("/home").text
        head = page[:page.index('id="preview"')] if 'id="preview"' in page else page
        # 保留"去聊天"的入口，但图标是聊天气泡、提示写"聊天"，不再叫"控制台"
        self.assertIn('href="/" title="聊天（控制台）" aria-label="聊天"', head)
        self.assertIn('<use href="#i-chat"/>', head)
        self.assertNotIn('title="控制台" aria-label="控制台"', head)
        self.assertIn('title="多窗口" aria-label="多窗口"', head)
        self.assertIn('title="远程主机" aria-label="远程主机"', head)
        self.assertIn('title="管理分享" aria-label="管理分享"', head)
        # 预览面板里的「在控制台打开」保留（那是选中某个 pane 后的入口）
        self.assertIn('id="pd-console"', page)

    def test_the_preview_panel_renders_markdown(self):
        """home 的悬浮/点击预览以前是 textContent，`## 标题`、表格、代码块全原样怼出来。"""
        page = self.client.get("/home").text
        self.assertIn("import {marked} from '/assets/marked.esm.js'", page)
        self.assertIn("window.__md = value => marked.parse(", page)
        # 原始 HTML 要转义（和 /preview 同一套策略）
        self.assertIn("marked.use({renderer:{html(token){return escHtml(", page)
        # 面板由共享模块建，渲染器由页面注入（首页用同一个 marked）
        self.assertIn("render:text=>window.__md?window.__md(text):String(text==null?'':text)", page)
        shared = self.client.get("/assets/pane-preview.js").text
        self.assertIn("body.innerHTML = options.render(preview.text);", shared)
        self.assertIn("body.classList.add('md');", shared)
        # markdown 模式下不能还保留 pre-wrap，并且代码/表格要有样式
        styles = self.client.get("/assets/pane-preview.css").text
        for marker in (".hp-body.md{white-space:normal}", ".hp-body.md pre{", ".hp-body.md table{"):
            self.assertIn(marker, styles)
        # 卡片上的 4 行摘要在不渲染 markdown 的前提下也要剥掉记号
        self.assertIn("function plainText(value){", page)
        self.assertIn("${esc(plainText(preview.text))}</p>", page)

    def test_renaming_a_pin_survives_a_second_click(self):
        """改名的输入框一失焦就 blur→保存→重绘，重绘会把紧随其后的 click 吞掉。

        所以钉子/铅笔/星星这些动作都改在 pointerdown 上执行，并且必须先收掉
        正在编辑的那一条（用每次改名自己的标记 mine，而不是共享的 state）。
        """
        script = self.client.get("/assets/recents.js").text
        self.assertIn("event.target.closest('[data-rename],[data-pin],[data-star]')", script)
        self.assertIn("event.preventDefault();                  // 别把焦点交给按钮", script)
        self.assertIn("if (pending && !pending.done && pending.commit) pending.commit(true);", script)
        self.assertIn("state.suppressClickAt = Date.now();      // 紧随其后的 click 不要再触发一遍", script)
        # 每次改名一个自己的标记，切到别的行不会把新标记误标成 done
        self.assertIn("const mine = {pane, done: false, commit: null};", script)
        self.assertIn("if (mine.done) return;", script)
        self.assertIn("if (state.renaming === mine) state.renaming = null;", script)
        # 这个 pointerdown 监听器不能是 passive（要 preventDefault）
        self.assertNotIn("}, {passive: true});\n    for (const type of ['pointerup'", script)

    def test_the_footer_keeps_clear_of_a_phones_bottom_left_corner(self):
        page = self.client.get("/").text
        self.assertIn(".side-foot{padding:10px 12px calc(10px + env(safe-area-inset-bottom)) calc(16px + env(safe-area-inset-left))}", page)

    def test_the_recent_block_has_an_adjustable_height_and_folds(self):
        """最近这块默认占侧栏 2/5，上边缘可拖，双击回默认；标题行能整个折叠。"""
        page = self.recent_script()
        styles = self.recent_styles()
        self.assertIn('id="recent-grip"', page)
        self.assertIn('id="recent-fold"', page)
        self.assertIn("height:var(--recent-h,40%)", styles)
        self.assertIn("const H_DEFAULT = 0.4", page)      # 2/5
        self.assertIn("H_MIN = 0.15", page)
        self.assertIn("H_MAX = 0.75", page)
        self.assertIn("localStorage.setItem(H_KEY", page)
        self.assertIn("localStorage.setItem(FOLD_KEY", page)
        self.assertIn(".recent.collapsed{height:auto", styles)
        self.assertIn(".recent.collapsed .recent-list{display:none}", styles)
        # 拖拽是 row-resize，向上拖变高；双击恢复默认
        self.assertIn("cursor:row-resize", styles)
        self.assertIn("state.drag.startH - (event.clientY - state.drag.startY)", page)
        self.assertIn("removeItem(H_KEY)", page)
        # 手机上把手更高一点，好按住
        self.assertIn(".recent-grip{top:-8px;height:16px}", styles)
        # 展开时把列表滚回顶部：折叠期间记住的滚动位置会把固定区推到视野外
        self.assertIn("if (!on && was && state.list) state.list.scrollTop = 0;", page)
        # 最近块会自己补数据（切回标签页/定时），重建时保住滚动位置
        self.assertIn("const keepScroll = state.list.scrollTop;", page)
        self.assertIn("state.list.scrollTop = keepScroll;", page)
        self.assertIn("Date.now() - lastAt > 15000", page)
        self.assertIn("setInterval(() => { if (!document.hidden) load(); }, 120000);", page)

    def test_the_console_opens_on_the_board_when_there_is_nothing_to_restore(self):
        page = self.client.get("/").text
        self.assertIn("landOnBoard", page)
        self.assertIn("location.replace('/home')", page)
        # 访问记录搬进共用模块，控制台在 selectPane 里调它
        self.assertIn("TmuxRecents.touch(id)", page)
        self.assertIn("body: JSON.stringify({pane})", self.recent_script())

    def test_every_host_row_offers_a_refresh(self):
        page = self.client.get("/home").text
        # the refresh button now comes from the shared shell, like /grid's
        self.assertIn('src="/assets/shell.js"', page)
        self.assertIn("TmuxShell.mount(", page)
        shell = self.client.get("/assets/shell.js").text
        for marker in ("data-refresh-host", "refreshHost", "class=\"trefresh\""):
            self.assertIn(marker, shell)
        console = self.client.get("/").text
        for marker in ("data-refresh-host", "refreshHost", "class=\"host-refresh\""):
            self.assertIn(marker, console)
        # both use the cheap per-host endpoint instead of a full re-probe
        # (the board's copy lives in shell.js now, the console's in its own page)
        self.assertIn("/api/hosts?refresh=1&host=", shell)
        self.assertIn("/api/hosts?refresh=1&host=", console)

    def test_the_board_sidebar_is_a_column_on_a_desktop_and_a_drawer_on_a_phone(self):
        """The phone drawer rules must stay inside their media query."""
        shell = self.client.get("/assets/shell.css").text
        import re as _re
        # every rule that turns the sidebar into an off-canvas drawer lives in the 860px query
        media = _re.search(r'@media \(max-width:860px\)\{(.*?)\n\s*\}\n', shell, _re.S)
        self.assertIsNotNone(media, "the drawer media query is missing")
        self.assertIn("position:fixed", media.group(1))
        self.assertIn("translateX(-100%)", media.group(1))
        # and no page may carry those rules loose (that made the column vanish at 1440px)
        for path in ("/home", "/grid"):
            page = self.client.get(path).text
            self.assertNotIn("position:fixed;top:0;left:0;z-index:20", page, path)

    def test_the_sidebar_width_is_draggable_and_remembered(self):
        home = self.client.get("/home")
        for marker in ('id="side-resizer"', "--side-w", 'src="/assets/shell.js"'):
            self.assertIn(marker, home.text)
        # the width key is now the shared one (was home-only tmux-home-side-w)
        self.assertIn("tmux-side-w", self.client.get("/assets/shell.js").text)
        # the drag styles live in the shared sidebar stylesheet now (board + grid)
        shell = self.client.get("/assets/shell.css").text
        for marker in ("col-resize", "width:min(86vw,320px)"):
            self.assertIn(marker, shell)
        self.assertIn('href="/assets/shell.css"', home.text)

        console = self.client.get("/")
        for marker in ('id="side-resizer"', "var(--side-w,280px)", "tmux-side-w"):
            self.assertIn(marker, console.text)
        # collapsing must still win over the stored width
        self.assertIn(".app.sidebar-collapsed{grid-template-columns:0 minmax(0,1fr)}", console.text)

        # a narrow sidebar must not lose its action buttons: the row stays on one line and scrolls
        # sideways instead of dropping icons onto a second row
        self.assertIn('<div class="side-actions">', home.text)
        shell_css = self.client.get("/assets/shell.css").text
        self.assertIn("flex-wrap:nowrap", shell_css)
        self.assertIn("overflow-x:auto", shell_css)
        self.assertNotIn("flex-wrap:wrap;row-gap:", shell_css)
        self.assertIn("initIconRowScroll", home.text)     # wheel scrolls the row sideways
        self.assertIn("flex-wrap:nowrap", console.text)
        self.assertNotIn("flex-wrap:wrap;row-gap:", console.text)
        self.assertIn("initIconRowScroll", console.text)

    def test_the_title_and_the_icon_row_are_stacked(self):
        """Title on its own line, the icons in one row underneath, with a fade as the "it scrolls" hint."""
        home = self.client.get("/home").text
        shell = self.client.get("/assets/shell.css").text
        self.assertIn(".side-head{position:relative;flex-direction:column;", shell)
        self.assertIn(".side-head::after{", shell)                       # background-colour fade
        self.assertIn("linear-gradient(to right,rgba(255,255,255,0),var(--surface,#fff))", shell)
        self.assertIn("flex-wrap:wrap", shell)                           # a PC wraps instead of scrolling
        # a desktop has no touch: no scrolling, the buttons shrink instead
        self.assertIn("@media (pointer:coarse){\n  .side-actions{flex-wrap:nowrap", shell)

        console = self.client.get("/?pane=%254").text
        self.assertIn(".brand{position:relative;flex-direction:column;", console)
        self.assertIn(".brand::after{", console)
        self.assertIn("linear-gradient(to right,rgba(238,237,232,0),var(--sidebar,#eeeDE8))", console)
        self.assertIn("@media (pointer:coarse){", console)
        self.assertIn(".brand-actions{flex-wrap:nowrap;overflow-x:auto;", console)   # phone: swipe

    def test_a_card_previews_first_and_the_panel_holds_the_ways_in(self):
        """Cards preview; the panel offers full preview / chat / close."""
        page = self.client.get("/home").text
        shared = self.client.get("/assets/pane-preview.js").text
        for marker in ('data-hp="full"', 'data-hp="chat"', 'data-hp="close"', '完整预览', '关闭'):
            self.assertIn(marker, shared)
        self.assertIn("showHover(cardEl,open.dataset.open,true)", page)   # click opens the panel
        self.assertNotIn('data-full=', page)                              # no per-card shortcut left
        # 没有预览内容时不弹空面板；有内容时预览面板自己给入口
        self.assertIn("if(!entry||!(entry.agent_preview||entry.preview))return;", page)
        self.assertIn("function previewFor(paneId)", shared)

    def test_the_tree_starts_collapsed_and_has_expand_buttons(self):
        """Clicking a server must not dump the whole tree open; the toolbar does that."""
        page = self.client.get("/home").text
        self.assertIn('id="tree-expand"', page)
        self.assertIn('id="tree-collapse"', page)
        # the open-branch choice is remembered by the shared shell now
        # (was home-only tmux-home-tree-open / state.treeOpen)
        shell = self.client.get("/assets/shell.js").text
        self.assertIn("tmux-tree-open", shell)              # the choice is remembered
        self.assertIn("state.open.has(key)", shell)        # collapsed by default
        self.assertNotIn("state.treeOpen.size?state.treeOpen.has(key):true", page)

        console = self.client.get("/").text
        self.assertIn('id="tree-expand"', console)
        self.assertIn('id="tree-collapse"', console)
        self.assertIn("function treeKeys()", console)

    def test_a_phone_tap_opens_the_conversation_instead_of_the_hover_panel(self):
        """Touch fires mouseover first, so the preview panel used to swallow the tap."""
        page = self.client.get("/home").text
        shared = self.client.get("/assets/pane-preview.js").text
        self.assertIn("const canHover = !!(window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches);", shared)
        self.assertIn("if (!canHover && !options.force) return;", shared)   # 手机默认不绑
        self.assertIn("const canHover=panePreview.canHover||TmuxPanePreview.hoverable;", page)
        self.assertIn("if(!canHover)return;", page)

    def test_the_full_preview_page_is_served_and_wired_to_the_board(self):
        preview = self.client.get("/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["cache-control"], "no-store")
        for marker in ("/api/panes/", "/interrupt", "Enter 发送", 'id="back"', "marked.esm.js"):
            self.assertIn(marker, preview.text)
        # chat/terminal sit next to the star and replace the old "open in console" button,
        # which pointed at the same place as the chat link
        for marker in ('id="open-chat"', 'id="open-terminal"', "i-chat", "i-terminal"):
            self.assertIn(marker, preview.text)
        self.assertNotIn('id="console"', preview.text)
        self.assertIn("mode=terminal", preview.text)

        page = self.client.get("/home").text
        for marker in ('id="preview-drawer"', 'id="pd-frame"', "PREVIEW_WIDE",
                       "openFullPreview", "closeFullPreview"):
            self.assertIn(marker, page)
        # the hover panel has to accept the pointer, otherwise it cannot be scrolled
        # (the panel itself is the shared /assets/pane-preview.css now)
        self.assertIn("pointer-events:auto;overscroll-behavior:contain",
                      self.client.get("/assets/pane-preview.css").text)

    def test_the_chat_and_terminal_switch_sits_next_to_the_star(self):
        page = self.client.get("/").text
        start = page.index('class="top-actions"')
        head = page[start:page.index("</header>", start)]
        self.assertIn('id="chat-mode"', head)
        self.assertIn('id="terminal-mode-button"', head)
        # the pair comes right before the star
        self.assertLess(head.index('id="chat-mode"'), head.index('id="fav-toggle"'))
        self.assertLess(head.index('id="terminal-mode-button"'), head.index('id="fav-toggle"'))
        # and is gone from the sidebar
        sidebar = page[page.index('class="sidebar"'):page.index('class="main"')]
        self.assertNotIn("mode-switch", sidebar)
        self.assertIn("aria-pressed", head)
        # a deep link may ask for the terminal view
        self.assertIn("get('mode')", page)
        self.assertIn("setMode('terminal')", page)

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
        self.tmux.transcript = lambda pane_id, limit=200, pane=None: {
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
        self.tmux.transcript = lambda pane_id, limit=200, pane=None: {
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
