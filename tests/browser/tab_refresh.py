"""Keep the actual update-toast reload attached to this tab's Chat."""
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests

ORIGIN = "http://tmuxweb.test"
WIDTHS = (1440, 1180, 1024, 900, 834, 768, 390)


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            first = dict(fixture.tmux.panes[0], id="%4", number=4, title="First Chat")
            second = dict(first, id="node~%4", number=125, title="Second Chat", prefix="node")
            local = {"id": "localhost", "name": "Local", "panes": [first]}
            remote = {"id": "node", "name": "Remote", "panes": [second]}
            browser = pw.chromium.launch()
            for width in WIDTHS:
                hosts = [local, remote]
                context = browser.new_context(viewport={"width": width, "height": 844},
                                              has_touch=width == 390, service_workers="block")

                def route(r):
                    path = urlsplit(r.request.url).path
                    if path == "/" or path.startswith("/assets/"):
                        resp = client.get(path)
                        return r.fulfill(status=resp.status_code, body=resp.content,
                                         content_type=resp.headers.get("content-type", "text/plain"))
                    if path == "/api/hosts":
                        return r.fulfill(json={"hosts": hosts})
                    if path == "/api/version":
                        return r.fulfill(json={"version": "new-fixture-version"})
                    return r.fulfill(json={"items": [], "panes": [], "messages": [], "prefs": {}})

                context.route("**/*", route)
                context.route_web_socket("**/*", lambda ws: None)
                context.add_init_script("""const schedule = window.setInterval;
                    window.setInterval = function(fn, ms, ...args) {
                      if (ms === 20000) window.pollHosts = fn;
                      return schedule.call(this, fn, ms, ...args);
                    };""")
                a, b = context.new_page(), context.new_page()
                a.goto(ORIGIN + "/?pane=4&view=test#anchor", wait_until="domcontentloaded")
                expect(a.locator("#selected-number")).to_have_text("#4")
                b.goto(ORIGIN + "/?pane=node~%254&mode=terminal", wait_until="domcontentloaded")
                expect(b.locator("#selected-number")).to_have_text("node#125")
                expect(b.locator("#terminal-mode-button")).to_have_attribute("aria-pressed", "true")
                a.bring_to_front()
                a.evaluate("() => { for(let n=0;n<3;n++) window.pollHosts(); }")
                with a.expect_navigation(wait_until="domcontentloaded"):
                    a.locator("#toast").get_by_role("button", name="刷新").click()
                expect(a.locator("#selected-number")).to_have_text("#4")
                expect(a.locator("#chat-mode")).to_have_attribute("aria-pressed", "true")
                assert parse_qs(urlsplit(a.url).query)["view"] == ["test"] and urlsplit(a.url).fragment == "anchor"
                # Cold restore while the other tab has most recently selected pane 4.
                hosts = [local]
                b.evaluate("localStorage.removeItem('tmuxweb.cache.hosts.v1')")
                b.reload(wait_until="domcontentloaded")
                expect(b.locator("#pane-count")).to_contain_text("1 panes")
                expect(b.locator("#selected-number")).to_be_hidden()
                hosts = [local, remote]
                b.bring_to_front()
                b.evaluate("window.pollHosts()")
                expect(b.locator("#selected-number")).to_have_text("node#125")
                expect(b.locator("#terminal-mode-button")).to_have_attribute("aria-pressed", "true")
                context.close()
                print(f"{width}px pass", flush=True)
            browser.close()
            assert not fixture.tmux.sent and not fixture.tmux.keys
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
