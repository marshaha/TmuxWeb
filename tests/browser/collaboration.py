"""Exercise the shipped drawer and Git review controls, with two fake panes."""
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    fixture.tmux.panes.append(dict(fixture.tmux.panes[0], id="%5", title="reviewer"))
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                revision = "a" * 64
                messages = []
                page = browser.new_page(viewport={"width": width, "height": 844},
                                        has_touch=width == 390, service_workers="block")
                def route(r):
                    req = r.request
                    uri = urlsplit(req.url)
                    path = uri.path
                    if path.endswith("/git"):
                        return r.fulfill(json={"repo": True, "changes": [{"path": "file.py", "code": "M"}]})
                    if path.endswith("/git/diff"):
                        return r.fulfill(json={"patch": "@@ -1 +1 @@\n-old\n+new", "revision": revision})
                    if path == "/api/relay/message":
                        messages.append(req.post_data_json)
                    if path in ("/", "/api/hosts") or path.startswith(("/assets/", "/api/relay/")):
                        full = path + ("?" + uri.query if uri.query else "")
                        res = client.post(full,json=req.post_data_json) if req.method=="POST" else client.get(full)
                        return r.fulfill(status=res.status_code,body=res.content,content_type=res.headers["content-type"])
                    return r.fulfill(json={"items":[],"messages":[],"panes":[],"prefs":{}})

                page.route("**/*", route)
                page.route_web_socket("**/*", lambda ws: ws.close())
                page.goto("http://tmuxweb.test/?pane=%254", wait_until="domcontentloaded")
                expect(page.locator("#agents-button")).to_be_enabled()
                def act(selector):
                    item = page.locator(selector)
                    item.tap() if width == 390 else item.click()
                page.locator("#git-open").evaluate("e=>e.click()")
                act("#gp-changes [data-gp-path]")
                act("#review-excerpt button:nth-child(2)")
                page.locator("#review-note").fill("verify new line")
                act("#review-add")
                expect(page.locator("#review-list")).to_contain_text("新文件 L1")
                revision = "b" * 64
                act("#review-send")
                expect(page.locator("#review-status")).to_contain_text("已变化")
                assert not messages
                act("#review-list button")
                act("#gp-changes [data-gp-path]")
                page.locator("#review-note").fill("review again")
                act("#review-add")
                page.locator("#review-target").select_option("%5")
                act("#review-send")
                expect(page.locator("#review-status")).to_contain_text("已提交")
                assert len(messages)==1 and messages[0]["target"]=="%5"
                act("#gp-close")
                page.evaluate("TmuxNeedsYouInbox.open()")
                act("#new-task")
                page.locator("#task-text").fill("check "+str(width))
                page.locator("#task-target").select_option("%5")
                act("#task-create")
                expect(page.locator("#task-status")).to_contain_text("已提交")
                page.goto("http://tmuxweb.test/?pane=%255",wait_until="domcontentloaded")
                expect(page.locator("#agents-button")).to_be_enabled()
                req=client.post("/api/relay/file-reviews", json={"requester":"other node", "target":"%5",
                    "operation":"write", "path":"results/out.txt"})
                assert req.status_code==200
                page.evaluate("TmuxNeedsYouInbox.open()")
                review=page.locator("#inbox-tasks article").filter(has_text="文件写入申请")
                expect(review).to_contain_text("results/out.txt")
                review.get_by_role("button",name="拒绝申请").click()
                expect(review).to_have_count(0)
                row=page.locator("#inbox-tasks article").filter(has_text="check "+str(width))
                expect(row).to_be_visible()
                row.get_by_role("button",name="开始执行").click()
                row.get_by_role("button",name="报告结果").click()
                page.locator("#task-result-text").fill("output "+str(width))
                act("#task-report button[value=delivered]")
                expect(row).to_have_count(0)
                act('[data-inbox-tab="results"]')
                result=page.locator("#inbox-results article").filter(has_text="output "+str(width))
                expect(result).to_be_visible()
                assert "relay=" in result.get_by_role("link").get_attribute("href")
                assert page.locator("#inbox-drawer button:visible").evaluate_all(
                    "els=>els.every(e=>e.scrollWidth<=e.clientWidth+1)")
                assert not fixture.tmux.keys
                page.close()
                print(f"{width}px pass",flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
