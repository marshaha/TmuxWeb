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
        for mode in ("strip", "grid2", "grid3"):
            self.assertIn(f'data-layout="{mode}"', page)
        self.assertIn("mode-grid", page)
        self.assertIn("--cols", page)
        self.assertIn("state.layout=layout.dataset.layout", page)
        self.assertIn("layout:state.layout", page)          # the choice is remembered

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
        # a shell has no transcript to chat with, so it opens as a terminal
        self.assertIn("AGENTS.has", page)

        term = self.client.get("/term?pane=%254")
        self.assertEqual(term.status_code, 200)
        for marker in ("/assets/xterm/xterm.js", "addon-fit.js", "/ws/terminal/",
                       "{type:'resize'", "{type:'input'", "embed"):
            self.assertIn(marker, term.text)

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
        self.assertIn('href="/grid"', self.client.get("/").text)
        self.assertIn('href="/grid"', self.client.get("/home").text)


if __name__ == "__main__":
    unittest.main()
