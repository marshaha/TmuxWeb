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

    def test_the_preview_page_can_be_embedded(self):
        page = self.client.get("/preview?pane=%254").text
        self.assertIn("body.embed header{display:none}", page)
        self.assertIn("get('embed')==='1'", page)

    def test_the_grid_is_linked_from_the_console_and_the_board(self):
        self.assertIn('href="/grid"', self.client.get("/").text)
        self.assertIn('href="/grid"', self.client.get("/home").text)


if __name__ == "__main__":
    unittest.main()
