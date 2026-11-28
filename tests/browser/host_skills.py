"""The host card reports the installed revision on desktop and phone."""
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import expect, sync_playwright

HTML = (Path(__file__).resolve().parents[2] / "web/hosts.html").read_text()
HOST = {"id":"m", "name":"macmini", "hostname":"macmini", "username":"tester", "port":22,
        "transport":"agent","agent_online":True, "skill_sync":{"count":12,"version":"abcdef01"*8,"ok":False}}

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (1440,1180,1024,900,834,768,390):
            page = browser.new_page(viewport={"width":width, "height":850})
            def route(r):
                path = urlsplit(r.request.url).path
                if path == "/hosts":
                    return r.fulfill(content_type="text/html",body=HTML)
                if path == "/api/remote-hosts":
                    return r.fulfill(json={"hosts":[HOST]})
                return r.fulfill(json={"nodes":[]})
            page.route("**/*", route)
            page.goto("http://fixture/hosts")
            card = page.locator("#list article")
            expect(card).to_contain_text("12 个 Skill · abcdef01 · 同步失败")
            assert card.evaluate("""el => {
                const r=el.getBoundingClientRect();
                return r.left >= 0 && r.right <= innerWidth &&
                  [...el.querySelectorAll('.agent-line,button')].filter(e=>e.offsetWidth)
                  .every(e=>e.scrollWidth<=e.clientWidth+1 && e.getBoundingClientRect().right<=r.right);
            }""")
            page.close()
            print(f"{width} px pass",flush=True)
        browser.close()

if __name__ == "__main__":
    main()
