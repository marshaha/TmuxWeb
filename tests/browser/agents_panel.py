"""Exercise the calls and events drawer with long lists and a delayed tab response."""
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def readable(page, selector):
    assert page.locator(selector).evaluate("""list => {
        const rows = [...list.children];
        const viewport = list.getBoundingClientRect();
        return viewport.height >= 44 && list.scrollHeight > list.clientHeight &&
            rows.every(row => {
                const head = row.firstElementChild;
                return head.offsetHeight >= 24 && row.clientHeight >= head.offsetHeight &&
                    head.scrollWidth <= head.clientWidth + 1;
            });
    }""")


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                page = browser.new_page(viewport={"width": width, "height": 844},
                                        has_touch=width == 390, service_workers="block")
                calls = [{"id": str(i), "child": "remote/" + "a"*50, "question": f"测试问答 {i}",
                          "status": "ok", "duration": 12} for i in range(40)]
                events = [{"id": str(i), "kind": "send", "actor": "ui", "status": "ok",
                           "summary": f"测试消息 {i}"} for i in range(60)]

                def route(r):
                    path = urlsplit(r.request.url).path
                    if path == "/api/agents/calls":
                        return r.fulfill(json={"calls": calls})
                    if path.startswith("/api/agents/calls/"):
                        return r.fulfill(json={"question": "完整问题", "reply": "完整回答"})
                    if path == "/api/events":
                        return r.fulfill(json={"events": events})
                    if path in ("/", "/api/hosts") or path.startswith("/assets/"):
                        res = client.get(path)
                        return r.fulfill(body=res.content, content_type=res.headers["content-type"])
                    return r.fulfill(json={"messages": [], "panes": [], "items": [], "prefs": {}})

                page.route("**/*", route)
                page.route_web_socket("**/*", lambda ws: ws.close())
                page.goto("http://tmuxweb.test/?pane=%254", wait_until="domcontentloaded")
                activate = "tap" if width == 390 else "click"
                def click(selector):
                    getattr(page.locator(selector).first, activate)()
                click("#agents-button")
                expect(page.locator("#agents-list .ag-call")).to_have_count(40)
                readable(page, "#agents-list")
                click("#agents-list .ag-head")
                expect(page.locator("#agents-list .ag-body")).to_contain_text("完整回答")
                click("#ag-tab-events")
                expect(page.locator("#events-list .ag-call")).to_have_count(60)
                expect(page.locator("#agents-list")).to_be_hidden()
                readable(page, "#events-list")
                page.locator("#events-list .ag-call").last.scroll_into_view_if_needed()
                expect(page.locator("#events-list .ag-call").last).to_be_in_viewport()
                if width == 390:
                    page.set_viewport_size({"width": 844, "height": 390})
                    readable(page, "#events-list")
                    page.set_viewport_size({"width": width, "height": 844})
                # Hold a poll even if its AbortSignal fires, so the stale-result guard is tested too.
                page.evaluate("""() => {
                    const original = window.fetch;
                    window.fetch = (url, opts) => {
                        if (!String(url).startsWith('/api/events?')) return original(url, opts);
                        return new Promise(resolve => window.finishEvents = () =>
                            resolve(new Response(JSON.stringify({events:[{id:'late',summary:'过期操作'}]}))));
                    };
                    window.TmuxAgents.refresh();
                }""")
                page.wait_for_function("typeof window.finishEvents === 'function'")
                calls.clear()
                click("#ag-tab-calls")
                expect(page.locator("#agents-empty")).to_contain_text("没有 relay 问答记录")
                page.evaluate("async () => { window.finishEvents(); await new Promise(r => setTimeout(r, 0)); }")
                expect(page.locator("#events-list")).to_be_hidden()
                expect(page.locator("#events-empty")).to_be_hidden()
                expect(page.locator("#events-list .ag-call")).to_have_count(0)
                expect(page.locator("#agents-count")).to_have_text("")
                click("#agents-close")
                click("#agents-button")
                expect(page.locator("#ag-tab-calls")).to_have_attribute("aria-selected", "true")
                page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
