"""Rotate a touch viewport with iPhone-sized cutouts and operate the pinned sidebar list."""
from pathlib import Path
import sys
from urllib.parse import urlsplit

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
            entries = [{"pane":f"%{n}", "number":n, "title":f"会话 {n} — long title",
                        "pinned":True, "visited_at":1} for n in range(125, 133)]
            def route(r):
                u = urlsplit(r.request.url)
                if u.path == "/api/home/recent":
                    return r.fulfill(json={"recent": entries})
                if u.path in ("/","/api/hosts") or u.path.startswith("/assets/"):
                    resp = client.get(u.path, params=u.query)
                    return r.fulfill(body=resp.content,content_type=resp.headers.get("content-type","text/plain"))
                return r.fulfill(json={"panes":[],"messages":[],"items":[],"prefs":{}})
            for width in (1440,1180,1024,900,834,768,390,874):
                page = browser.new_page(viewport={"width":390,"height":844},has_touch=True,
                                        is_mobile=True,service_workers="block")
                page.add_init_script("localStorage.setItem('tmux-side-w', '240')")
                page.route("**/*", route)
                page.route_web_socket("**/*",lambda ws:None)
                page.goto("http://tmuxweb.test/?pane=%254",wait_until="domcontentloaded")
                height = 844 if width == 390 else 402
                page.set_viewport_size({"width":width,"height":height})
                if width != 390:
                    page.context.new_cdp_session(page).send("Emulation.setSafeAreaInsetsOverride",
                        {"insets":{"left":62,"right":62,"bottom":21,"top":0}})
                if width == 390 or page.locator(".app.sidebar-collapsed").count():
                    page.locator("#menu").click()
                if width != 390:
                    page.wait_for_function("""() => document.querySelector('#sidebar').getBoundingClientRect().width >= 200 &&
                        !document.querySelector('.app').getAnimations().length""")
                sidebar = page.locator("#sidebar")
                rows = sidebar.locator(".recent-row.pinned")
                expect(rows).to_have_count(len(entries))
                if width != 390:
                    expect(sidebar.locator("#recent-block")).to_have_css("padding-left","62px")
                    expect(sidebar.locator("#recent-block")).to_have_css("padding-right","6px")
                head = sidebar.locator(".recent-title")
                expect(head).to_be_in_viewport()
                assert head.evaluate("e => e.getBoundingClientRect().height < 22 && e.scrollWidth <= e.clientWidth + 1")
                assert rows.first.evaluate("""e => {
                    const box=e.getBoundingClientRect(), label=e.querySelector('.label').getBoundingClientRect();
                    const buttons=[...e.querySelectorAll('button')].map(x=>x.getBoundingClientRect());
                    return label.width>=42 && label.right<=buttons[1].left+1 &&
                        buttons.every((b,i)=>b.left>=box.left-1 && b.right<=box.right+1 &&
                            (!i || b.left>=buttons[i-1].right-1));
                }"""), width
                last = rows.last.get_by_role("button",name="改名")
                last.scroll_into_view_if_needed()
                last.click()
                expect(rows.last.locator("input")).to_be_visible()
                page.close()
                print("874×402 rotation pass" if width == 874 else f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__=="__main__":
    main()
