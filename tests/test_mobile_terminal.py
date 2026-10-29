"""The phone terminal: Termius-style one-row key bar, extended pad, no visible input field."""
import unittest

from fastapi.testclient import TestClient

from tmux_web import create_app
from tests.test_tmux_web import FakeRemoteStore, FakeSSHRunner, FakeTmux
from tests.fake_terminal import FakeTerminalFactory

PASSWORD = "terminal-ui-test"


class MobileTerminalTests(unittest.TestCase):
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
        self.page = self.client.get("/").text

    def test_there_is_no_separate_input_box_below_the_terminal(self):
        """The keyboard types straight into the terminal; a visible mirror field only got in the way."""
        self.assertNotIn('id="terminal-input-row"', self.page)
        self.assertIn('class="terminal-kb-input"', self.page)
        # it still has to be a real field so the soft keyboard opens, sized 16px so iOS does not zoom
        self.assertIn("font-size:16px;opacity:0;pointer-events:none", self.page)

    def test_the_key_bar_is_one_row_with_a_pinned_cluster(self):
        for marker in ('data-key="BackTab"', 'data-key="Escape">esc</button>', 'data-mod="ctrl"',
                       'data-mod="alt"', 'data-ctrl-key="c"', 'id="terminal-paste"',
                       'class="terminal-key-scroll"', 'class="terminal-key-pinned"',
                       'id="terminal-kb"', 'id="terminal-more"'):
            self.assertIn(marker, self.page)

    def test_the_extended_pad_replaces_the_soft_keyboard(self):
        self.assertIn('id="terminal-ext"', self.page)
        self.assertIn("ext-open", self.page)
        self.assertIn("data-paste", self.page)
        # opening it hides the system keyboard, otherwise the terminal would be squeezed to nothing
        self.assertIn("if(open)$('terminal-input')?.blur()", self.page)

    def test_tapping_the_terminal_opens_the_keyboard(self):
        """A tap used to blur the field on purpose; typing now starts where you tap."""
        self.assertIn("field.focus({preventScroll:true});", self.page)
        self.assertIn("},true);", self.page)          # xterm stops propagation, so capture first
        self.assertIn("addEventListener('touchstart'", self.page)

    def test_the_keyboard_target_is_editable_so_ios_drops_its_form_assistant_bar(self):
        """iOS shows up/down/Done above the keyboard for form controls; contenteditable is not one."""
        self.assertIn("function useEditableCapture()", self.page)
        self.assertIn("setAttribute('contenteditable','true')", self.page)
        self.assertIn("KB_MODE==='input'", self.page)               # escape hatch (read once at boot)
        self.assertIn("field.isContentEditable", self.page)
        self.assertIn("inputType!=='deleteContentBackward'", self.page)   # backspace on empty

    def test_pinch_zooms_the_font_and_it_is_remembered(self):
        self.assertIn("setupTerminalZoom", self.page)
        self.assertIn("tmux-term-font", self.page)
        self.assertIn("setTerminalFont", self.page)

    def test_ios_asks_before_summoning_the_system_keyboard(self):
        """iOS puts an unremovable native bar above the keyboard, so there the keyboard is opt-in."""
        self.assertIn("function keyboardOnTerminalTap()", self.page)
        self.assertIn("if(!keyboardOnTerminalTap())return;", self.page)
        self.assertIn("forced==='auto'", self.page)
        self.assertIn("forced==='manual'", self.page)
        self.assertIn("/iP(hone|ad|od)/.test(navigator.userAgent)", self.page)

    def test_a_deep_link_survives_a_slow_remote_scan(self):
        """?pane= used to be applied on the first /api/hosts reply, before remote panes existed."""
        self.assertIn("state.wantedPane=wanted||null", self.page)
        self.assertIn("function applyWantedPane()", self.page)


if __name__ == "__main__":
    unittest.main()
