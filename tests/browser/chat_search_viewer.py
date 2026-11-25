"""Exercise search and image viewing through the served Chat UI on an isolated 500-message pane."""
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
            pane = dict(fixture.tmux.panes[0], id="%4", number=125, provider="hermes")
            photo = "long-photo-name-" * 5 + ".png"
            messages = [dict(id=str(i), role="user", text="搜" * 512) for i in range(500)]
            messages[-1]["text"] += '\nuser uploaded file “' + photo + '”'
            messages[-2].update(role="assistant", text="搜" * 512 + "\n\n![diagram](/assets/fixture-image.svg)")
            def route(r):
                path = urlsplit(r.request.url).path
                if path.endswith(photo) or path.endswith("fixture-image.svg"):
                    return r.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="850"><rect width="1200" height="850" fill="teal"/></svg>')
                if path == "/" or path.startswith("/assets/"):
                    res = client.get(path)
                    return r.fulfill(status=res.status_code,body=res.content,content_type=res.headers["content-type"])
                if path == "/api/hosts":
                    return r.fulfill(json={"hosts":[{"id":"localhost","panes":[pane]}]})
                if path.endswith("/transcript"):
                    return r.fulfill(json={"messages":messages,"provider":"hermes","signature":"test"})
                return r.fulfill(json={"items":[],"panes":[],"prefs":{},"messages":[]})
            for width in (1440,1180,1024,900,834,768,390):
                page = browser.new_page(viewport={"width":width,"height":844},has_touch=width==390,service_workers="block")
                page.route("**/*",route)
                page.route_web_socket("**/*",lambda ws: None)
                page.goto("http://fixture.test/?pane=%254",wait_until="domcontentloaded")
                expect(page.locator("#transcript-messages article")).to_have_count(500)
                page.locator("#chat-search-button").click()
                expect(page.locator("#cs-count")).to_be_empty()
                page.evaluate("""() => {
                    window.changes = 0;
                    new MutationObserver(rs => changes+=rs.length).observe(document.querySelector('#transcript-messages'),
                        {subtree:true,childList:true,characterData:true});
                }""")
                field = page.locator("#cs-input")
                field.fill("搜")
                expect(page.locator("#cs-count")).to_have_text("1 / 256000")
                field.press("Shift+Enter")
                expect(page.locator("#cs-count")).to_have_text("256000 / 256000")
                page.wait_for_timeout(450)
                assert page.evaluate("changes") == 0
                assert page.evaluate("CSS.highlights.get('cs-hit').size <= 96 && CSS.highlights.get('cs-current').size === 1")
                assert page.locator("*").count() <= 12000
                page.evaluate("""() => document.querySelector('#transcript-messages .chat-bubble').firstChild.appendData('搜')""")
                expect(page.locator("#cs-count")).to_have_text("256001 / 256001")
                field.fill("does-not-match")
                expect(page.locator("#cs-count")).to_have_text("没有命中")
                field.press("Escape")
                assert page.evaluate("CSS.highlights.size === 0")
                thumb = page.locator("a.upload-thumb")
                thumb.click()
                dialog = page.get_by_role("dialog",name=photo)
                expect(dialog).to_be_visible()
                dialog.locator("img").evaluate("img => img.decode()")
                bounds = dialog.bounding_box()
                assert bounds["x"] >= width*.05 and bounds["y"] >= 844*.06, bounds
                assert dialog.evaluate("""d => d.scrollWidth<=d.clientWidth+1 &&
                    [...d.querySelectorAll('button,a')].every(el=>el.scrollWidth<=el.clientWidth+1)""")
                expect(dialog.locator("img")).to_have_attribute("src",thumb.get_attribute("href"))
                assert page.evaluate("parseFloat(getComputedStyle(document.querySelector('#iview'),'::backdrop').backgroundColor.split(',').at(-1))<0.4")
                dialog.get_by_role("button",name="放大图片").click()
                expect(dialog.get_by_role("button",name="还原图片")).to_have_attribute("aria-pressed","true")
                zoomed = dialog.bounding_box()
                assert all(abs(zoomed[k]-bounds[k]) < 1 for k in bounds), (bounds, zoomed)
                if width == 390:
                    page.touchscreen.tap(6,6)
                else:
                    page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                inline = page.locator('#transcript-messages img[alt="diagram"]')
                inline.click()
                expect(page.get_by_role("dialog",name="diagram")).to_be_visible()
                page.go_back()
                expect(page.locator("#iview")).to_be_hidden()
                expect(inline).to_be_in_viewport()
                page.close()
                print(f"{width}px pass",flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
