"""Measure the rendered key rows and exercise touch input against a simulated WebSocket."""
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests

WIDTHS = (1440, 1180, 1024, 900, 834, 768, 390)


def rows(page, selector):
    keys = page.locator(selector)
    result = keys.evaluate("""el => {
        const buttons = [...el.querySelectorAll('button')].filter(b => b.offsetWidth);
        return {
          rows: new Set(buttons.map(b => Math.round(b.getBoundingClientRect().top))).size,
          fits: buttons.every(b => b.scrollWidth <= b.clientWidth + 1 && b.scrollHeight <= b.clientHeight + 1),
          height: el.offsetHeight
        };
    }""")
    assert result["fits"] and result["rows"] <= 2 and result["height"] <= 100, result
    return result["rows"]


def swipe(page, locator):
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] - 25, box["y"] + box["height"] / 2
    session = page.context.new_cdp_session(page)
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for distance in (12, 35, 70, 120, 175):
        session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x-distance, "y": y}]})
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    session.detach()


def check_first_page(page, selector, inputs):
    group = page.locator(selector)
    buttons = group.locator("button").all()[:7]
    assert [button.inner_text() for button in buttons] == ["↑", "↓", "←", "→", "Enter", "Tab", "Esc"]
    # display:contents on standalone desktop gives the group no box of its own.
    assert group.evaluate("""el => {
        const clip = (el.clientWidth ? el : el.parentElement).getBoundingClientRect();
        return [...el.children].slice(0,7).every(b => {
            const r=b.getBoundingClientRect();
            return r.width && r.left >= clip.left && r.right <= clip.right + 1 &&
                r.right <= window.innerWidth + 1 && b.clientWidth + 1 >= b.scrollWidth;
        });
    }""")
    start = len(inputs)
    for button in buttons:
        button.tap()
    assert inputs[start:] == ["\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C", "\r", "\t", "\x1b"]


def console_keyboard(page, inputs):
    controls = page.locator("#terminal-keys")
    expect(controls).to_be_visible()
    assert rows(page, "#terminal-keys") == 1
    check_first_page(page, ".terminal-key-scroll", inputs)
    inputs.clear()
    host = page.locator("#terminal-host")
    height = host.bounding_box()["height"]
    page.locator("#terminal-kb").tap()
    assert rows(page, "#terminal-keys") == 2
    jump = page.locator("#terminal-jump")
    jump.evaluate("b => b.hidden = false")
    assert jump.bounding_box()["y"] + jump.bounding_box()["height"] <= controls.bounding_box()["y"]
    jump.evaluate("b => b.hidden = true")
    pad = page.locator("#terminal-type")
    pad.locator('[data-type-char="q"]').tap()
    pad.get_by_label("下一组按键").tap()
    pad.locator('[data-type-char="z"]').tap()
    pad.locator('[data-type-key="shift"]').tap()
    pad.locator('[data-type-char="Q"]').tap()
    pad.get_by_label("下一组按键").tap()
    pad.locator('[data-type-key="digits"]').tap()
    pad.locator('[data-type-char="7"]').tap()
    pad.get_by_label("下一组按键").tap()
    for key in ("space", "enter", "back"):
        pad.locator(f'[data-type-key="{key}"]').last.tap()
    expect(page.locator("#terminal-more")).to_be_visible()
    page.locator("#terminal-more").tap()
    expect(pad).to_be_hidden()
    assert rows(page, "#terminal-keys") == 2
    assert height - host.bounding_box()["height"] < 50
    ext = page.locator("#terminal-ext")
    ext.evaluate("el => el.addEventListener('scrollend', () => el.dataset.settled='yes', {once:true})")
    controls.get_by_role("button", name="符号", exact=True).tap()
    expect(page.locator("#terminal-group-symbol")).to_be_visible()
    assert rows(page, "#terminal-keys") == 2
    swipe(page, ext)
    page.wait_for_function("document.querySelector('#terminal-ext').scrollLeft > 0")
    expect(ext).to_have_attribute("data-settled", "yes")
    ext.locator('[data-text="@"]').tap()
    controls.get_by_role("button", name="控制", exact=True).tap()
    ext.locator('[data-ctrl-key="c"]').tap()
    controls.get_by_role("button", name="导航", exact=True).tap()
    ext.locator('[data-key="End"]').tap()
    page.locator("#terminal-more").tap()
    assert rows(page, "#terminal-keys") == 1
    assert inputs == ["q", "z", "Q", "7", " ", "\r", "\x7f", "@", "\x03", "\x1b[F"], inputs


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            for width in WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": 844}, has_touch=True, service_workers="block")
                inputs = []

                def route(route):
                    path = urlsplit(route.request.url).path
                    if path in ("/", "/term", "/api/hosts") or path.startswith("/assets/"):
                        result = client.get(path, params=urlsplit(route.request.url).query)
                        return route.fulfill(status=result.status_code, body=result.content,
                                             content_type=result.headers.get("content-type", "application/octet-stream"))
                    return route.fulfill(json={"items": [], "messages": [], "panes": [], "prefs": {}})

                def socket(ws):
                    if "/ws/terminal/" in ws.url:
                        def record(raw):
                            frame = json.loads(raw)
                            if frame.get("type") == "input":
                                inputs.append(frame["data"])
                        ws.on_message(record)

                page.route("**/*", route)
                page.route_web_socket("**/*", socket)
                page.goto("http://tmuxweb.test/?pane=%254&kb=custom", wait_until="domcontentloaded")
                page.locator("#terminal-mode-button").click()
                expect(page.locator("#terminal-host .xterm")).to_be_visible()
                if width == 390:
                    console_keyboard(page, inputs)
                else:
                    expect(page.locator("#terminal-keys")).to_be_hidden()
                for query in ("", "&embed=1&reply=1"):
                    page.goto("http://tmuxweb.test/term?pane=%254" + query)
                    expect(page.locator("#dot")).to_have_class("dot live")
                    assert rows(page, "#keys") == 1
                    check_first_page(page, ".keys-main", inputs)
                    page.locator('[data-key="enter"]').tap()
                    assert inputs[-1] == "\r"
                    if width == 390 and not query:
                        page.locator("#keys-more").tap()
                        assert rows(page, "#keys") == 2
                        page.locator('[data-key="ctrlc"]').tap()
                        assert inputs[-1] == "\x03"
                        page.locator("#keys-more").tap()
                        assert rows(page, "#keys") == 1
                    if width == 390 and query:
                        page.set_viewport_size({"width": 220, "height": 230})
                        assert rows(page, "#keys") == 1
                        assert page.locator("#host").bounding_box()["height"] > page.viewport_size["height"] * 0.6
                        assert page.locator("#keys").evaluate("el => el.scrollWidth > el.clientWidth")
                page.close()
                print(f"{width}px pass", flush=True)
            assert not fixture.tmux.keys and not fixture.tmux.sent
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
