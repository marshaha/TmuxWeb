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
        self.assertIn('id="grid-size"', page)
        self.assertIn("GRID_PRESETS", page)
        for shape in ((2, 1), (2, 2), (3, 2), (2, 3), (3, 3)):
            self.assertIn(f'{{cols:{shape[0]},rows:{shape[1]}}}', page)
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
        self.assertIn(".grip{display:none}", page.replace(",.grip", ".grip"))
        self.assertIn(".splitter,.grip{display:none}", page)

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
