"""The multi-window page: several conversations side by side, resizable and remembered."""
import unittest

from fastapi.testclient import TestClient

from tmux_web import create_app
from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTmux
from tests.fake_terminal import FakeTerminalFactory

PASSWORD = "grid-test-password"


class GridPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(
            FakeTmux(),
            password=PASSWORD,
            session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(),
            share_db=":memory:",
            home_db=":memory:",
            remote_store=FakeRemoteStore(),
            remote_runner_factory=FakeSSHRunner,
        ))
        self.client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    def test_the_grid_requires_a_login_and_comes_back_after_it(self):
        anonymous = TestClient(create_app(
            FakeTmux(), password=PASSWORD, session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:", home_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        ))
        response = anonymous.get("/grid", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?next=/grid")

        page = self.client.get("/grid")
        self.assertEqual(page.status_code, 200)
        self.assertIn("多窗口", page.text)

    def test_every_conversation_page_needs_a_login(self):
        """The pages themselves used to be public; all of them now bounce to /login."""
        anonymous = TestClient(create_app(
            FakeTmux(), password=PASSWORD, session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:", home_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        ))
        for path, expected in (("/home", "/login?next=/home"),
                               ("/preview?pane=%254", "/login?next=/preview%3Fpane%3D%25254"),
                               ("/", None)):
            response = anonymous.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303, path)
            if expected:
                self.assertEqual(response.headers["location"], expected)

    def test_each_cell_embeds_a_preview_and_the_layout_is_remembered(self):
        page = self.client.get("/grid").text
        self.assertIn("/preview?pane=", page)
        self.assertIn("embed=1", page)
        self.assertIn("tmux-grid-v1", page)          # local storage key
        self.assertIn("data-split", page)            # draggable splitters
        self.assertIn("data-remove", page)
        self.assertIn("min-width:240px", page)       # a cell never collapses

    def test_widths_are_keyed_by_pane_so_adding_a_window_never_pins_a_column(self):
        """Widths used to be a positional array: the first window kept the width it had alone."""
        page = self.client.get("/grid").text
        self.assertIn("state.widths[pane]", page)
        self.assertIn("delete state.widths[paneId]", page)
        self.assertIn("flex:1 1 0;min-width:240px", page)   # unset columns share the space

    def test_the_layout_can_be_switched_between_one_row_and_a_grid(self):
        page = self.client.get("/grid").text
        for mode in ("strip", "grid"):
            self.assertIn(f'data-layout="{mode}"', page)
        self.assertIn("mode-grid", page)
        self.assertIn("state.layout=layout.dataset.layout", page)
        self.assertIn("layout:state.layout", page)          # the choice is remembered
        # the two old fixed grids still load as a plain grid
        self.assertIn("layout==='grid2'||layout==='grid3'", page)

    def test_a_grid_can_be_any_rows_by_columns_and_every_cell_resizes(self):
        page = self.client.get("/grid").text
        # 形状不再是硬编码的 chip 列表，而是一个 6×6 的挑格子（见 shapeGrid()）
        self.assertIn('id="shape-btn"', page)
        self.assertIn('id="shape-grid"', page)
        self.assertIn('id="shape-label"', page)
        self.assertIn("SHAPE_MAX_COLS", page)
        self.assertIn("data-cols=", page)
        self.assertIn("data-rows=", page)
        # cells are sized by fr ratios so a dragged seam keeps the rest proportional
        self.assertIn("--col-tpl", page)
        self.assertIn("--row-tpl", page)
        self.assertIn("grid-template-columns:var(--col-tpl", page)
        self.assertIn("grid-template-rows:var(--row-tpl", page)
        self.assertIn('class="grip col"', page)
        self.assertIn('class="grip row"', page)
        self.assertIn("colFr:state.colFr", page)            # ratios are remembered too
        self.assertIn("GRID_MIN", page)
        # a column never shrinks into a sliver, and the phone stays single-column
        self.assertIn("const perPx=sum/total", page)
        self.assertIn(".splitter,.grip,.cell .rh{display:none}", page)

    def test_the_shape_picker_also_takes_a_custom_rows_by_columns(self):
        page = self.client.get("/grid").text
        # 自定义弹窗：列/行两个输入 + 应用
        self.assertIn('id="grid-custom"', page)
        self.assertIn('id="gc-cols"', page)
        self.assertIn('id="gc-rows"', page)
        self.assertIn('id="gc-apply"', page)
        # 形状按钮里始终有一个「自定义…」，点开就是列/行输入
        self.assertIn("自定义…", page)
        self.assertIn('id="shape-custom"', page)
        self.assertIn("state.custom", page)
        # 上限放宽到 6×6，窗口多时行数自动长（不是静默裁掉）
        self.assertIn("SHAPE_MAX_COLS", page)
        self.assertIn("SHAPE_MAX_ROWS", page)
        self.assertIn("custom:state.custom||null", page)     # 自定义形状跟着布局一起记住
        self.assertIn("updateShapeHint()", page)             # 输入时就告诉用户要几行
        # 点同一个形状不该抹掉手动拖出来的比例；换形状才重置
        self.assertIn("if(!same){", page)
        self.assertIn("if(opener)opener.focus()", page)

    def test_the_canvas_mode_lets_every_window_be_moved_and_resized(self):
        """画布模式：窗口是自由摆的矩形，拖标题栏移动、拖边角缩放，位置大小都记住。"""
        page = self.client.get("/grid").text
        self.assertIn("LAYOUTS=['strip','grid','canvas']", page)
        self.assertIn('data-layout="canvas"', page)
        self.assertIn("画布", page)
        # 最小尺寸 + 吸附距离 + 参考线的延迟
        for token in ("CANVAS_MIN_W", "CANVAS_MIN_H", "SNAP=6", "GUIDE_DELAY"):
            self.assertIn(token, page)
        # 8 个缩放手柄 + 标题栏拖动
        for direction in ("n", "s", "w", "e", "nw", "ne", "sw", "se"):
            self.assertIn(f'data-resize="{direction}"', page)
        self.assertIn("function attachHandles(", page)
        self.assertIn("cell.classList.add('dragging')", page)
        # 参考线是延迟出现的，松手就清空（不是一直抖）
        self.assertIn("function queueGuides(", page)
        self.assertIn("function hideGuides(", page)
        self.assertIn("hideGuides();", page)
        # 位置/大小/形状跟着布局一起存
        self.assertIn("cells:state.cells", page)
        self.assertIn("canvasCols:state.canvasCols", page)
        self.assertIn("canvasRows:state.canvasRows", page)
        # 一排按钮里能整理、能微调
        self.assertIn('id="tidy"', page)
        self.assertIn("ArrowLeft", page)
        # 手机上画布退回单列（绝对定位会把超出屏幕的窗口全藏起来）
        self.assertIn(".grid.mode-canvas .cell{position:relative!important", page)

    def test_a_phone_can_move_between_stacked_windows(self):
        """手机竖屏：外壳必须正好一屏，否则 .grid 的 overflow 不触发，只能看到最后一格。"""
        page = self.client.get("/grid").text
        self.assertIn(".shell{height:100dvh;min-height:0;overflow:hidden}", page)
        # 画布不再给 #grid 设内联宽高：绝对定位的窗口自己撑出滚动范围
        self.assertNotIn("grid.style.width=area.width", page)
        self.assertNotIn("if(x+w+16>grid.clientWidth)", page)
        # iframe 会吃掉竖向手势，所以窄屏多窗口时给一个翻页器
        self.assertIn('id="grid-pager"', page)
        self.assertIn('data-page="-1"', page)
        self.assertIn("function syncPager(", page)
        self.assertIn("function bindPager(", page)
        self.assertIn("scroll-snap-type:y proximity", page)
        # 按钮小（26px）但热区外扩，且点击/lastTap 去重 + scrollTo 失败时直接赋值
        self.assertIn(".grid-pager button{display:grid;place-items:center;width:26px;height:26px", page)
        self.assertIn(".grid-pager button::after{content:'';position:absolute;inset:-5px", page)
        # 贴着 iPhone 右下角的圆角会被切掉：抬到安全区之上
        self.assertIn("bottom:calc(18px + env(safe-area-inset-bottom,0px))", page)
        self.assertIn("right:calc(10px + env(safe-area-inset-right,0px))", page)
        self.assertIn("touch-action:manipulation", page)
        self.assertIn("if(dir===lastDir&&now-lastTap<500)return;", page)
        self.assertIn("try{grid.scrollTo({top:target,behavior:'smooth'});}catch{grid.scrollTop=target;}", page)
        self.assertIn("pager.addEventListener('pointerup'", page)     # 有些 WebView 不派发 click

    def test_the_canvas_still_falls_back_to_one_column_on_a_phone(self):
        """桌面上画布靠内联 width/height 撑大，手机上必须让位给单列，否则横向溢出。"""
        page = self.client.get("/grid").text
        self.assertIn(".grid.mode-canvas{display:flex;flex-direction:column;overflow-y:auto;width:auto!important", page)
        # 画布不再靠内联宽高撑大，手机上也就没有需要清掉的东西
        self.assertNotIn("grid.style.width=(x+w+16)+'px'", page)
        self.assertIn("grid.style.removeProperty('width')", page)
        self.assertIn("grid.style.removeProperty('height')", page)

    def test_tiling_measures_the_viewport_not_the_grown_canvas(self):
        """画布被拖大以后，平铺不能拿被撑大的宽度去算，否则窗口越铺越小。"""
        page = self.client.get("/grid").text
        self.assertIn("function tileCanvas(cols,rows){", page)
        self.assertIn("box.style.removeProperty('width');box.style.removeProperty('height');", page)

    def test_a_new_window_on_the_canvas_lands_in_the_next_empty_slot(self):
        """加窗口不能把它扔到画布最底下、也不能把用户摆好的位置整体重排。"""
        page = self.client.get("/grid").text
        self.assertIn("function slotRect(", page)
        self.assertIn("function ensureCanvasCells(", page)
        # 只有"还是自动平铺的样子"才整体重排，动过手脚的画布保持不动
        self.assertIn("const pure=tiled&&", page)
        self.assertIn("if(!tiled||pure){tileCanvas(cols,rows);return;}", page)
        self.assertIn("state.cells[pane]=slotRect(state.panes.indexOf(pane)", page)

    def test_changing_the_shape_never_reloads_the_embedded_windows(self):
        """Reloading an iframe throws away a live terminal and re-fetches the chat; keep the nodes."""
        page = self.client.get("/grid").text
        self.assertIn("const cells=new Map()", page)
        self.assertIn("function cellFor(", page)
        # only touch an iframe when the pane is new or switches chat <-> terminal
        self.assertIn("const switching=cell&&cell.dataset.mode!==mode", page)
        # and only move a cell when it is genuinely out of order -- insertBefore reloads otherwise
        self.assertIn("const order=[...grid.querySelectorAll(':scope > .cell')]", page)
        self.assertNotIn("grid.innerHTML=state.panes.map(", page)

    def test_switching_between_canvas_and_grid_never_remounts_a_window(self):
        """切换布局不能重新插入 cell —— 重插一次 iframe 就等于把终端/聊天重新加载一遍。"""
        page = self.client.get("/grid").text
        # 顺序只在 .cell 之间比较（参考线层、徽标、分隔线都不是窗口）
        self.assertIn("const order=[...grid.querySelectorAll(':scope > .cell')]", page)
        self.assertIn("if(ref!==cell)grid.insertBefore(cell,ref)", page)
        self.assertNotIn("if(grid.children[index]!==cell)grid.insertBefore(", page)
        # 退出画布时把参考线层真删掉，否则它夹在 cell 之间同样会打乱下标
        self.assertIn("layer.classList.remove('show');layer.innerHTML='';layer.remove();", page)

    def test_the_window_picker_gets_its_hosts_from_the_shared_sidebar(self):
        """The page only builds the picker from state.hosts; the shell callback has to fill it."""
        page = self.client.get("/grid").text
        self.assertIn("onHosts: hosts => {", page)
        self.assertIn("state.hosts=hosts||[]", page)
        self.assertIn("if($('picker').open)renderPicker(", page)

    def test_sidebar_buttons_are_sized_on_every_page(self):
        """An inline <svg> with no size renders at 300px: that was the giant gear on /grid."""
        shell = self.client.get("/assets/shell.css").text
        self.assertIn(".chip-btn{", shell)
        self.assertIn(".chip-btn .ico{", shell)
        self.assertIn("width:12px;height:12px", shell)

    def test_the_embedded_copy_button_keeps_its_label_inside_the_capsule(self):
        preview = self.client.get("/preview?pane=%254&embed=1").text
        self.assertIn(".copy{flex:0 0 auto;white-space:nowrap;", preview)
        # .ico is display:block in the preview: it has to be inline inside the button
        self.assertIn(".copy .ico{display:inline-block;", preview)

    def test_a_window_can_be_dragged_to_a_new_position(self):
        page = self.client.get("/grid").text
        self.assertIn("body.moving iframe{pointer-events:none}", page)   # iframes must not eat the drag
        self.assertIn("drop-target", page)
        self.assertIn("state.panes.splice(to,0,pane)", page)

    def test_offscreen_windows_back_off_and_resume(self):
        """A window scrolled out of the grid must stop polling every 2 seconds."""
        page = self.client.get("/grid").text
        self.assertIn("tmuxweb:'visibility'", page)
        self.assertIn("addEventListener('scroll',queueVisibility", page)

        preview = self.client.get("/preview?pane=%254").text
        self.assertIn("data.tmuxweb!=='visibility'", preview)
        self.assertIn("cellVisible?2000:15000", preview)

    def test_a_window_can_be_chat_or_a_real_terminal(self):
        page = self.client.get("/grid").text
        self.assertIn('data-mode-toggle', page)
        self.assertIn("/term?pane=", page)
        self.assertIn("function setMode(", page)
        self.assertIn("modes:state.modes", page)
        # chat is the default when the pane really has a transcript; a plain shell degrades to the
        # terminal instead of showing an empty chat. Restoring a preset may set it too.
        self.assertEqual(page.count("state.modes[pane]='term'"), 2)   # default + preset restore
        self.assertIn("capabilities.transcript===false", page)
        self.assertIn("function defaultModeFor(", page)
        # a manual toggle (either direction) outranks the default and is remembered per pane
        self.assertIn("state.explicit[pane]=true", page)
        self.assertIn("for(const [pane,on] of Object.entries(saved.explicit||{}))", page)

        term = self.client.get("/term?pane=%254")
        self.assertEqual(term.status_code, 200)
        for marker in ("/assets/xterm/xterm.js", "addon-fit.js", "/ws/terminal/",
                       "{type:'resize'", "{type:'input'", "embed"):
            self.assertIn(marker, term.text)

    def test_a_phone_gets_a_compact_toolbar_and_one_card_per_screen(self):
        page = self.client.get("/grid").text
        for marker in (".top .lbl{display:none}", ".top .count{display:none}",
                       "height:calc(100dvh - 64px)", "scroll-snap-align:start", ".seg{display:none}"):
            self.assertIn(marker, page)
        # an unsized inline <svg> keeps its 300px default and spills out of the 30px button
        shell = self.client.get("/assets/shell.css").text
        self.assertIn(".icon-btn .ico{width:16px;height:16px}", shell)
        # the drawer's shadow must not bleed into a closed phone drawer
        self.assertIn(".shell.mobile-open .sidebar{transform:none;box-shadow:6px 0 24px rgba(38,37,32,.12)}", shell)
        # a message column must not stretch the chat horizontally on a narrow screen
        preview = self.client.get("/preview?pane=%254&embed=1").text
        self.assertIn(".msg>*{min-width:0;max-width:100%}", preview)
        # a pane without a transcript shows its screen and a way into the terminal, not a dead card
        self.assertIn("function enterScreenMode(", preview)
        self.assertIn("切到终端", preview)

    def test_the_terminal_page_needs_a_login(self):
        anonymous = TestClient(create_app(
            FakeTmux(), password=PASSWORD, session_secret="test-secret",
            terminal_factory=FakeTerminalFactory(), share_db=":memory:", home_db=":memory:",
            remote_store=FakeRemoteStore(), remote_runner_factory=FakeSSHRunner,
        ))
        response = anonymous.get("/term?pane=%254", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?next=/term%3Fpane%3D%25254")

    def test_double_click_maximises_a_window_without_reloading_it(self):
        page = self.client.get("/grid").text
        self.assertIn("maximized", page)
        self.assertIn("addEventListener('dblclick'", page)
        self.assertIn("toggleMax", page)
        # maximise is a class toggle, not a re-render: re-rendering would drop a live terminal
        self.assertIn("grid.classList.toggle('maximized'", page)

    def test_arrangements_can_be_saved_as_presets(self):
        page = self.client.get("/grid").text
        for marker in ('id="preset-btn"', 'id="preset-save"', "tmux-grid-presets-v1",
                       "data-apply", "state.presets[name]="):
            self.assertIn(marker, page)

    def test_polling_the_pane_list_does_not_rebuild_the_cells(self):
        """loadMeta() runs every 30s; a full render there would reload every iframe."""
        page = self.client.get("/grid").text
        self.assertIn("if(!renamed){", page)
        self.assertIn("state.titles.clear()", page)

    def test_the_preview_page_can_send_its_pane_to_the_grid(self):
        page = self.client.get("/preview?pane=%254").text
        self.assertIn('id="open-grid"', page)
        self.assertIn("'/grid?add='", page)

    def test_the_grid_uses_the_same_sidebar_shell_as_the_board(self):
        page = self.client.get("/grid").text
        for marker in ('href="/assets/shell.css"', 'src="/assets/shell.js"',
                       'id="sidebar"', 'id="tree"', 'id="side-collapse"', 'id="side-open"',
                       'id="tree-expand"', 'id="tree-collapse"', 'id="drawer-backdrop"',
                       "TmuxShell.mount("):
            self.assertIn(marker, page)
        # clicking a session row puts that pane in a window
        self.assertIn("onPane: paneId => openPane(paneId)", page)
        # and the shell fetches /api/hosts, so the page must not do it twice
        self.assertNotIn("loadMeta\n", page)
        self.assertIn("if(window.TmuxShell)", page)

        shell = self.client.get("/assets/shell.js").text
        for marker in ("window.TmuxShell", "function renderTree", "tmux-tree-open",
                       "api/home/favorites", "/api/panes/status"):
            self.assertIn(marker, shell)

    def test_the_tree_shows_which_sessions_are_already_windows(self):
        page = self.client.get("/grid").text
        for marker in ("function syncTreeMarks()", "TmuxShell.markActive(state.panes)",
                       "function openPane(paneId)", "function flashPane(paneId)", "cell.flash"):
            self.assertIn(marker, page)
        shell = self.client.get("/assets/shell.js").text
        self.assertIn("markActive", shell)
        self.assertIn(".tpane.in-grid", self.client.get("/assets/shell.css").text)
        # clicking a row that is already a window must not add it twice
        self.assertIn("if(state.panes.includes(paneId)){flashPane(paneId);return;}", page)

    def test_the_console_can_push_its_pane_into_the_grid(self):
        console = self.client.get("/").text
        self.assertIn('id="grid-add"', console)
        self.assertIn("location='/grid?add='", console)
        self.assertIn("params.get('add')", self.client.get("/grid").text)

    def test_the_preview_page_can_be_embedded(self):
        page = self.client.get("/preview?pane=%254").text
        self.assertIn("body.embed header{display:none}", page)
        self.assertIn("get('embed')==='1'", page)

    def test_the_grid_is_linked_from_the_console_and_the_board(self):
        console = self.client.get("/").text
        self.assertIn('href="/grid"', console)
        self.assertIn('href="/home"', console)
        # the grid sits next to the home icon in the console's sidebar header
        self.assertIn('href="/grid" title="多窗口（几个会话并排）"', console)
        self.assertIn('href="/home" title="主页（收藏与看板）"', console)
        self.assertLess(console.index('href="/home" title="主页（收藏与看板）"'),
                        console.index('href="/grid" title="多窗口（几个会话并排）"'))
        self.assertIn('href="/grid"', self.client.get("/home").text)


if __name__ == "__main__":
    unittest.main()
