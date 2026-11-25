"""Cursor evidence appears and clears without sending any pane input."""
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_cursor_notice import ROWS, frame
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            fixture.tmux.panes[0].update(command="kimi", provider="kimi", pane_pid="123")
            fixture.tmux.output = "\n".join(ROWS)
            fixture.tmux.capture_ansi = lambda *args, **kwargs: fixture.tmux.output
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                cursor = {"y": 1}
                fixture.tmux.cursor_frame = lambda actual: frame(y=cursor["y"])
                page = browser.new_page(viewport={"width": width, "height": 844},
                                        has_touch=width == 390, service_workers="block")

                def route(r):
                    url = urlsplit(r.request.url)
                    if (url.path in ("/", "/api/hosts") or url.path.startswith("/assets/")
                            or url.path.endswith("/tail") or url.path.startswith("/api/needs-you/inbox")):
                        resp = client.request(r.request.method, url.path, params=url.query)
                        return r.fulfill(status=resp.status_code, body=resp.content,
                                         content_type=resp.headers.get("content-type", "application/octet-stream"))
                    return r.fulfill(json={"panes": [], "items": [], "messages": [], "prefs": {}})

                page.route("**/*", route)
                page.route_web_socket("**/*", lambda ws: None)
                page.goto("http://tmuxweb.test/?pane=%254", wait_until="domcontentloaded")
                card = page.locator("#interaction-inline #prompt-card")
                expect(card.locator("#prompt-card-title")).to_have_text("终端可能正在等你选择", timeout=15000)
                expect(card).to_contain_text(ROWS[0])
                expect(card.locator("#prompt-card-confirm")).to_be_hidden()
                expect(card.get_by_role("option")).to_have_count(0)
                for el in (card.locator("#prompt-card-title"), card.locator("#prompt-card-hint")):
                    assert el.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                page.evaluate("TmuxNeedsYouInbox.open()")
                row = page.locator("#inbox-list .inbox-item", has_text=ROWS[0])
                expect(row).to_have_count(1)
                cursor["y"] = 5  # Input history uses the same screen with the caret back in the editor.
                expect(row).to_have_count(0, timeout=16000)
                expect(card).to_be_hidden()
                assert not fixture.tmux.sent and not fixture.tmux.keys
                page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
