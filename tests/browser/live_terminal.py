"""Check default expansion, ANSI colors, collapse and retries on busy and idle panes."""
from pathlib import Path
import sys
from urllib.parse import quote, unquote, urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            original = fixture.tmux.panes[0]
            panes = [dict(original, id=f"%{n}", pane_pid=str(1000+n), number=n,
                          command="codex", provider="codex", title=f"Codex {n}") for n in (41, 42)]
            panes[1].update(command="opencode", provider=None, title="OpenCode")
            prompt = {"kind": "select", "source": "hermes_clarify", "verified": True,
                      "read_only": False, "revision": "fixture-question", "title": "选择工具",
                      "question_count": 2, "options": [{"label": "A"}, {"label": "B"}]}
            panes[0].update(command="hermes", provider="hermes", title="Hermes")
            fixture.tmux.panes = panes
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                page = browser.new_page(viewport={"width": width, "height": 844}, has_touch=width == 390, service_workers="block")
                reads = []
                fixture.tmux.keys.clear()

                def route(r):
                    path = unquote(urlsplit(r.request.url).path)
                    if path == "/" or path.startswith("/assets/"):
                        res = client.get(path)
                        return r.fulfill(status=res.status_code, body=res.content,
                                         content_type=res.headers.get("content-type", "application/octet-stream"))
                    if path.endswith("/key"):
                        # TestClient unquotes its path twice, unlike the live ASGI server.
                        res = client.post(quote(urlsplit(r.request.url).path), json=r.request.post_data_json)
                        return r.fulfill(status=res.status_code, json=res.json())
                    if path == "/api/hosts":
                        return r.fulfill(json={"hosts": [{"id": "localhost", "name": "Local", "panes": panes}]})
                    if path == "/api/panes/status":
                        return r.fulfill(json={"panes": [{"id": "%41", "status": "busy", "needs_input": True, "prompt": prompt},
                                                         {"id": "%42", "status": "idle"}]})
                    if path == "/api/panes/%42/transcript":
                        return r.fulfill(json={"messages": []})
                    if path.endswith("/transcript"):
                        return r.fulfill(json={"messages": [{"role": "assistant", "id": "1", "text": "已开始"}],
                                               "provider": "hermes", "waiting": False, "signature": "v1"})
                    if path.endswith("/tail"):
                        pane = path.split("/")[-2]
                        assert "color=1" in r.request.url
                        reads.append(pane)
                        if pane == "%42" and reads.count(pane) == 1:
                            return r.fulfill(status=503, json={"detail": "offline"})
                        text = pane + " <img> " + "X"*200
                        if pane == "%42":
                            return r.fulfill(json={"digest": pane, "text": text})
                        color = 196 if reads.count(pane) == 1 else 46
                        return r.fulfill(json={"digest": str(color), "text": text,
                                               "ansi": f"\x1b[38;5;{color}m{text}\x1b[0m"})
                    return r.fulfill(json={"panes": [], "messages": [], "prefs": {}, "favorites": [], "items": []})

                page.add_init_script("localStorage.setItem('tmux-interaction-placement','manual')")
                page.route("**/*", route)
                page.route_web_socket("**/*", lambda ws: ws.close())
                page.goto("http://tmuxweb.test/?pane=%2541", wait_until="domcontentloaded")
                toggle = page.locator("#live-tail-toggle")
                fragment = page.locator("#chat-activity #live-tail-text")
                expect(fragment).to_contain_text("%41 <img>", timeout=15000)
                expect(fragment.locator("span").first).to_have_css("color", "rgb(255, 0, 0)")
                expect(fragment.locator("span").first).to_have_css("color", "rgb(0, 255, 0)", timeout=6000)
                assert fragment.locator("img").count() == 0
                assert fragment.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                assert toggle.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                nav = page.locator("#chat-activity #prompt-card-nav")
                expect(page.locator("#inspect-interaction")).to_be_hidden()
                expect(page.locator("#interaction-placement")).to_have_value("inline")
                expect(nav).to_be_visible()
                expect(nav.get_by_role("button", name="下一题")).to_be_visible()
                assert nav.evaluate("""el => [...el.children].filter(b => !b.hidden).every(b =>
                    b.scrollWidth <= b.clientWidth + 1 && b.scrollHeight <= b.clientHeight + 1)""")
                keys = page.locator("#live-keys-nav")
                opener = page.locator("#live-keyboard-toggle")
                expect(keys).to_be_hidden()
                expect(opener).to_have_attribute("aria-expanded", "false")
                folded_height = page.locator("#live-tail-surface").bounding_box()["height"]
                opener.click()
                expect(keys).to_be_visible()
                assert page.locator("#live-tail-surface").bounding_box()["height"] > folded_height
                assert keys.bounding_box()["x"] >= opener.bounding_box()["x"] + opener.bounding_box()["width"]
                opener.click()
                expect(keys).to_be_hidden()
                opener.focus()
                page.keyboard.press("Enter")
                expect(opener).to_have_attribute("aria-expanded", "true")
                assert keys.evaluate("""el => el.closest('#live-tail-surface').firstElementChild.id === 'live-tail-text'""")
                buttons = keys.get_by_role("button")
                assert buttons.all_text_contents() == ["↑", "↓", "←", "→", "Tab", "Enter",
                                                        "Esc", "Shift+Tab", "Space", "Ctrl+C", "Ctrl+D"]
                assert keys.evaluate("""el => {
                    const box = el.getBoundingClientRect();
                    const buttons = [...el.children];
                    return buttons.every(b => b.offsetTop === buttons[0].offsetTop &&
                        b.scrollWidth <= b.clientWidth + 1) &&
                        buttons.slice(0, 7).every(b => b.getBoundingClientRect().right <= box.right + 1);
                }""")
                assert not fixture.tmux.keys
                expected_keys = ["Up", "Down", "Left", "Right", "Tab", "Enter", "Escape", "BTab", "Space", "C-c", "C-d"]
                for button in buttons.all():
                    with page.expect_response(lambda r: r.url.endswith("/key") and r.ok):
                        button.tap() if width == 390 else button.click()
                assert fixture.tmux.keys == [("%41", key) for key in expected_keys]
                toggle.click()
                expect(fragment).to_be_hidden()
                expect(keys).to_be_hidden()
                before = len(reads)
                page.wait_for_timeout(1700)
                assert len(reads) == before
                page.locator("#menu").click()
                page.locator("#pane-search").fill("#42")
                page.locator('button[data-pane="%42"]').click()
                expect(page.locator("#selected-number")).to_contain_text("42")
                expect(nav).to_be_hidden()
                expect(toggle).to_have_attribute("aria-expanded", "true")
                expect(page.locator(".transcript-empty")).to_be_visible()
                expect(fragment).to_contain_text("暂时无法读取终端")
                # A host refresh must not reapply the original deep link after a pane switch.
                with page.expect_response(lambda r: "/api/hosts?refresh=1" in r.url):
                    page.locator('[data-refresh-host="localhost"]').first.evaluate("button => button.click()")
                expect(page.locator("#selected-number")).to_contain_text("42")
                expect(fragment).to_contain_text("%42 <img>", timeout=10000)
                assert "%41" not in fragment.inner_text()
                assert reads.count("%42") == 2
                assert fragment.locator("span").count() == 0
                expect(keys).to_be_hidden()
                opener.click()
                expect(keys).to_be_visible()
                with page.expect_response(lambda r: r.url.endswith("/key") and r.ok):
                    keys.get_by_role("button", name="Enter").click()
                assert fixture.tmux.keys[-1] == ("%42", "Enter")
                assert not fixture.tmux.sent and not page.locator("#live-tail iframe").count()
                page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
