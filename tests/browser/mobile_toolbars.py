"""Check all three sidebar toolbars and the composer at the supported widths."""
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests

MODEL = "openai/gpt-5.2-codex-extra-long-model-name"
WIDTHS = (1440, 1180, 1024, 900, 834, 768, 390)


def sidebar(page, console):
    frame = page.locator(".sidebar-tool-frame")
    row = frame.locator(":scope > div")
    close = page.get_by_role("button", name="折叠侧边栏", exact=True)
    open_button = page.locator("#menu" if console else "#side-open")
    if open_button.is_visible():
        open_button.click()
    expect(row.locator("[data-plugin-button]")).to_have_count(10)
    expect(frame).to_have_class(re.compile("more-right"))
    assert row.evaluate("el => new Set([...el.children].map(b => b.offsetTop)).size === 1")
    close.click(trial=True)
    initial = close.bounding_box()
    if page.viewport_size["width"] == 390:
        box = row.bounding_box()
        x, y = box["x"] + box["width"] - 15, box["y"] + box["height"] / 2
        touch = page.context.new_cdp_session(page)
        touch.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for distance in (16, 45, 80, 120):
            touch.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x-distance, "y": y}]})
        touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        touch.detach()
    else:
        row.hover()
        page.mouse.wheel(0, 160)
    page.wait_for_function("() => document.querySelector('.sidebar-tool-frame > div').scrollLeft > 0")
    row.locator("a").last.focus()
    expect(frame).not_to_have_class(re.compile("more-right"))
    expect(frame).to_have_class(re.compile("more-left"))
    assert abs(close.bounding_box()["x"] - initial["x"]) < 1
    close.click()
    expect(open_button).to_be_visible()


def manual_inspection(page):
    button = page.locator("#inspect-interaction")
    placement = page.locator("#interaction-placement")
    expect(button).to_be_hidden()
    expect(placement.locator('option[value="manual"]')).to_be_disabled()

    def change_setting(enabled):
        page.evaluate("window.TmuxSettings.open()")
        lab = page.locator("#ts-lab-toggle")
        if lab.get_attribute("aria-expanded") != "true":
            lab.click()
        switch = page.get_by_role("switch", name="手动查看交互")
        expect(switch).to_have_attribute("aria-checked", str(not enabled).lower())
        assert switch.evaluate("""el => {
            const row = el.parentNode, r = row.getBoundingClientRect();
            return row.scrollWidth <= row.clientWidth + 1 && el.getBoundingClientRect().right <= r.right;
        }""")
        switch.click()
        expect(switch).to_have_attribute("aria-checked", str(enabled).lower())
        page.locator("#ts-close").click()

    expect(placement).to_be_hidden()
    page.evaluate("localStorage.setItem('tmux-interaction-placement','popup')")
    page.reload(wait_until="domcontentloaded")
    expect(placement).to_be_hidden()
    expect(placement).to_have_value("inline")
    change_setting(True)
    expect(placement).to_be_visible()
    expect(placement).to_have_value("popup")
    placement = page.get_by_label("交互卡片显示方式", exact=True)
    placement.select_option("popup")
    if page.viewport_size["width"] == 390:
        expect(page.locator("#placement-short")).to_have_text("浮窗")
    expect(page.locator("#interaction-popup #interaction-cards")).to_have_count(1)
    placement.select_option("inline")
    expect(page.locator("#interaction-inline #interaction-cards")).to_have_count(1)
    expect(button).to_be_visible()
    placement.select_option("manual")
    page.reload(wait_until="domcontentloaded")
    expect(placement).to_have_value("manual")
    button.click()
    expect(page.locator("#prompt-card")).to_be_visible()
    change_setting(False)
    expect(placement).to_be_hidden()
    expect(button).to_be_hidden()
    expect(page.locator("#prompt-card")).to_be_hidden()
    expect(placement).to_have_value("inline")
    page.locator("#model-toggle").click()
    page.get_by_role("button", name="打开原生模型菜单").click()
    expect(page.locator("#chat-reply-panel iframe")).to_be_visible()
    expect(page.locator("#prompt-card")).to_be_hidden()
    page.locator("#chat-reply-close").click()
    expect(page.locator("#live-tail-toggle")).to_be_focused()


def composer(page, width):
    actions = page.locator(".top-actions")
    if width == 390:
        expect(actions).to_have_class(re.compile("scrolls"))
    if "scrolls" in actions.get_attribute("class").split():
        assert actions.evaluate("""el => {
            const edge=getComputedStyle(el), icon=getComputedStyle(el.querySelector('.share-btn'));
            return edge.borderTopLeftRadius === icon.borderTopLeftRadius &&
                edge.borderBottomLeftRadius === icon.borderBottomLeftRadius &&
                parseFloat(icon.borderTopLeftRadius) > 0 && edge.boxShadow !== 'none';
        }""")
    expect(page.locator("#submit-enter")).to_have_count(0)
    if width == 390:
        expect(page.locator(".composer-foot")).to_be_hidden()
        bottom = page.locator(".composer-card").bounding_box()
        assert 0 <= 844 - bottom["y"] - bottom["height"] <= 8
    page.evaluate("() => { for (let i=0; i<3; i++) window.versionPoll(); }")
    toast = page.locator("#toast")
    expect(toast).to_have_text("有新版本刷新稍后")
    assert toast.evaluate("""el => {
      const box = el.getBoundingClientRect();
      return box.left >= 0 && box.right <= innerWidth && [...el.children].every(child => {
        const r = child.getBoundingClientRect();
        return r.left >= box.left && r.right <= box.right && child.scrollWidth <= child.clientWidth + 1;
      });
    }""")
    if width == 390:
        assert toast.bounding_box()["y"] < page.locator("#input").bounding_box()["y"]
    toast.get_by_role("button", name="稍后").click()
    expect(toast).to_be_hidden()
    model = page.locator("#model-toggle")
    model.click()
    expect(model).to_contain_text(MODEL)
    first_menu = page.locator("#model-menu").bounding_box()
    assert first_menu["x"] >= 0 and first_menu["x"] + first_menu["width"] <= width
    page.locator("#model-filter").press("Escape")
    assert model.bounding_box()["height"] <= 40
    bar = page.locator(".cf-bar").bounding_box()
    expect(page.locator("#live-tail-head #inspect-interaction")).to_be_hidden()
    for selector in ("#attach", "#model-toggle"):
        ctl = page.locator(selector)
        box = ctl.bounding_box()
        assert box["x"] >= bar["x"] - 1 and box["x"] + box["width"] <= bar["x"] + bar["width"] + 1, selector
        assert abs(box["y"] + box["height"]/2 - bar["y"] - (bar["height"]-7)/2) <= 2, selector
        assert ctl.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), selector
    model.click()
    menu = page.locator("#model-menu")
    expect(menu).to_be_visible()
    expect(menu).to_contain_text(MODEL)
    box = menu.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    page.locator("#model-filter").press("Escape")
    with page.expect_file_chooser():
        page.get_by_role("button", name="添加附件", exact=True).click()
    field = page.locator("#input")
    field.fill("first")
    field.press("Enter" if width == 390 else "Shift+Enter")
    field.press("End")
    field.press("x")
    expect(field).to_have_value("first\nx")
    with page.expect_request(lambda req: req.method == "POST" and req.url.endswith("/input")) as sent:
        if width == 390:
            page.get_by_role("button", name="发送", exact=True).tap()
        else:
            field.press("Enter")
    assert sent.value.post_data_json == {"text": "first\nx", "submit": True}
    expect(field).to_have_value("")
    if width == 390:
        expect(page.locator("#menu")).to_be_visible()


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            pane = dict(fixture.tmux.panes[0], provider="codex", command="codex")
            buttons = [dict(label=f"插件 {n}", icon="grid", href="/grid", plugin=str(n)) for n in range(10)]

            def route(r):
                path = urlsplit(r.request.url).path
                if path in ("/", "/home", "/grid") or path.startswith("/assets/"):
                    resp = client.get(path)
                    return r.fulfill(status=resp.status_code, body=resp.content,
                                     content_type=resp.headers.get("content-type", "application/octet-stream"))
                if path == "/api/prefs":
                    response = (client.put(path, json=r.request.post_data_json) if r.request.method == "PUT"
                                else client.get(path))
                    return r.fulfill(status=response.status_code, json=response.json())
                if path.endswith("/prompt"):
                    return r.fulfill(json={"prompt": None, "screen": ["Fixture"]})
                if path == "/api/version":
                    return r.fulfill(json={"version": "next-fixture-build"})
                if path == "/api/plugins":
                    return r.fulfill(json={"buttons": buttons})
                if path == "/api/hosts":
                    return r.fulfill(json={"hosts": [{"id": "localhost", "name": "本机", "panes": [pane]}]})
                if path.endswith("/transcript"):
                    return r.fulfill(json={"provider": "codex", "model": MODEL, "messages": [{"id": "1", "role": "assistant", "text": "完成"}]})
                if path.endswith("/model"):
                    return r.fulfill(json={"command": "/model"})
                if path.endswith("/models"):
                    return r.fulfill(json={"current": MODEL, "current_provider": "openai", "presets": [MODEL], "native_picker": True})
                return r.fulfill(json={"items": [], "panes": [], "messages": [], "favorites": [], "prefs": {}})

            for width in WIDTHS:
                for url in ("/?pane=%254", "/home", "/grid"):
                    page = browser.new_page(viewport={"width": width, "height": 844}, has_touch=width == 390,
                                            service_workers="block")
                    page.add_init_script("""const schedule = window.setInterval;
                        window.setInterval = function(fn, delay, ...args) {
                          if (delay === 20000) window.versionPoll = fn;
                          return schedule.call(this, fn, delay, ...args);
                        };""")
                    page.route("**/*", route)
                    page.route_web_socket("**/*", lambda ws: ws.close())
                    page.goto("http://tmuxweb.test" + url, wait_until="domcontentloaded")
                    if url.startswith("/?"):
                        manual_inspection(page)
                        composer(page, width)
                    sidebar(page, url.startswith("/?"))
                    page.close()
                print(f"{width}px pass", flush=True)
            assert not fixture.tmux.sent and not fixture.tmux.keys
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
