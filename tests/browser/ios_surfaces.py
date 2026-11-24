"""Check the nine page entry points with the shared navigation and accessibility styles."""
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    paths = {"index": "/", "grid": "/grid", "home": "/home", "hosts": "/hosts",
             "shares": "/shares", "share": "/share/layoutfixture", "preview": "/preview",
             "term": "/term", "login": "/login"}
    try:
        with fixture.client as client, sync_playwright() as pw:
            login = client.get("/login")
            fixture.login()
            created = client.post("/api/shares", json={"pane_id":"%4", "slug":"layoutfixture",
                                                       "password":"", "expires_in":3600})
            created.raise_for_status()
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                def route(r):
                    path = urlsplit(r.request.url).path
                    if path == "/api/hosts":
                        return r.fulfill(json={"hosts":[{"id":"localhost","panes":fixture.tmux.panes}]})
                    if path in paths.values() or path.startswith(("/assets/", "/share-assets/")):
                        response = login if path == "/login" else client.get(path)
                        return r.fulfill(body=response.content, status=response.status_code,
                                         content_type=response.headers.get("content-type", "text/html"))
                    return r.fulfill(json={"hosts":[], "panes":[], "prefs":{}, "messages":[], "shares":[], "items":[]})

                for screen, path in paths.items():
                    page = browser.new_page(viewport={"width":width,"height":844}, has_touch=width == 390,
                                            service_workers="block", reduced_motion="reduce")

                    page.route("**/*", route)
                    page.route_web_socket("**/*", lambda ws: None)
                    page.goto("http://tmuxweb.test" + path + ("?pane=%254" if screen in ("index","term","preview") else ""), wait_until="domcontentloaded")
                    expect(page.locator("html")).to_have_attribute("data-screen", screen)
                    size = page.evaluate("""() => ({
                        doc: document.documentElement.scrollWidth, view: innerWidth,
                        radius: getComputedStyle(document.documentElement).getPropertyValue('--ui-radius').trim(),
                        overflow: [...document.querySelectorAll('button')].filter(b =>
                          b.checkVisibility({opacityProperty:true,visibilityProperty:true}) && b.clientWidth &&
                          b.scrollWidth>b.clientWidth+1 && (() => {
                            const r=b.getBoundingClientRect();
                            return b.contains(document.elementFromPoint(r.x+r.width/2, r.y+r.height/2));
                          })())
                          .map(b => b.id || b.textContent.trim().slice(0,40))})""")
                    assert size["doc"] <= size["view"]+1 and not size["overflow"], (screen,width,size)
                    assert size["radius"] == "18px", (screen,width,size)
                    control = page.locator("button:visible").first
                    if control.count():
                        expect(control).to_have_css("transition-duration", "0s")
                    if width == 390 and screen == "index":
                        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                        expect(page.locator("html")).to_have_attribute("data-theme", "night")
                        page.emulate_media(color_scheme="light", reduced_motion="reduce")
                        expect(page.locator("html")).not_to_have_attribute("data-theme", "night")
                        session = page.context.new_cdp_session(page)
                        session.send("Emulation.setEmulatedMedia", {"features":[
                            {"name":"prefers-reduced-transparency","value":"reduce"},
                            {"name":"prefers-reduced-motion","value":"reduce"}]})
                        expect(page.locator(".app .topbar")).to_have_css("backdrop-filter", "none")
                        session.detach()
                    page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
