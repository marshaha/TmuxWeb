"""Exercise the served Chat form and authenticated POST with two browser contexts and a native owner fixture."""
import asyncio
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_native_forms import Owner, exchange
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            pane = dict(fixture.tmux.panes[0], provider="hermes", command="python3", pane_pid="1201")
            fixture.tmux.panes = [pane]
            def route(r):
                path = urlsplit(r.request.url).path
                if path == "/" or path.startswith("/assets/") or path == slug:
                    response = (client.post(path, json=r.request.post_data_json) if r.request.method=="POST"
                                else client.get(path))
                    return r.fulfill(body=response.content, status=response.status_code,
                                     content_type=response.headers.get("content-type","text/html"))
                if path == "/api/panes/status":
                    return r.fulfill(status=503,json={"detail":"status sweep unavailable"})
                if path == "/api/hosts":
                    return r.fulfill(json={"hosts":[{"id":"localhost","panes":[pane]}]})
                if path.endswith("/transcript"):
                    return r.fulfill(json={"provider":"hermes","messages":[{"id":"u","role":"user",
                        "text":"历史示例：选择 provider？ 1. openai 2. claude"}]})
                return r.fulfill(json={"panes":[],"messages":[],"items":[],"prefs":{}})
            for number, width in enumerate((1440,1180,1024,900,834,768,390), 1):
                pane_id = "%" + str(number)
                pane["id"] = pane_id
                slug = "/api/panes/%25" + str(number) + "/native-form"
                owner = Owner()
                delayed = width == 390
                pending = owner.open
                if delayed:
                    owner.open = []
                def helper(actual, pid, submission, runner):
                    assert (actual,pid,runner) == (pane_id,"1201",None)
                    return asyncio.run(exchange(owner,submission))
                with patch("hermes_forms.on_host", side_effect=helper):
                    pages = []
                    for _ in range(2):
                        page=browser.new_page(viewport={"width":width,"height":844},has_touch=width==390,
                                              service_workers="block")
                        page.route("**/*",route)
                        page.route_web_socket("**/*",lambda ws:None)
                        page.goto("http://tmuxweb.test/?pane=%25"+str(number),wait_until="domcontentloaded")
                        pages.append(page)
                    page, second=pages
                    card=page.locator("#transcript article.assistant#native-form")
                    if delayed:
                        expect(page.locator("#live-tail-surface")).to_be_visible()
                        expect(card).to_be_hidden()
                        owner.open = pending
                    expect(card.get_by_role("heading",name="Hermes 等你回答")).to_be_in_viewport(timeout=22000)
                    expect(page.locator("#live-tail-surface")).to_be_hidden()
                    page.locator("#live-tail-toggle").click()
                    expect(page.locator("#live-tail-surface")).to_be_visible()
                    page.locator("#live-tail-toggle").click()
                    expect(card).to_be_visible()
                    option=card.get_by_label("claude",exact=True)
                    option.tap() if width==390 else option.click()
                    page.reload(wait_until="domcontentloaded")
                    expect(option).to_be_checked()
                    model=card.get_by_label("默认模型",exact=True)
                    model.focus()
                    model.press("Space")
                    expect(model).to_be_checked()
                    for p in pages:
                        assert p.locator("#native-form").evaluate("""el => el.scrollWidth<=el.clientWidth+1 &&
                            [...el.querySelectorAll('button,label,legend')].every(x=>x.scrollWidth<=x.clientWidth+1)""")
                    other=second.locator("#native-form")
                    other.get_by_label("openai",exact=True).check()
                    other.get_by_label("默认模型",exact=True).check()
                    cmd=card.get_by_role("button",name="提交全部答案")
                    cmd.tap() if width==390 else cmd.click()
                    expect(card.get_by_role("status")).to_have_text("已回答")
                    other.get_by_role("button",name="提交全部答案").click()
                    expect(other.get_by_role("status")).to_contain_text("已结束")
                    assert owner.answers == [{"outcome":"submitted","answers":{"provider":"claude","model":"默认模型"}}]
                    page.reload(wait_until="domcontentloaded")
                    expect(page.get_by_text("历史示例",exact=False).first).to_be_visible()
                    expect(card).to_be_hidden()
                    assert fixture.tmux.keys==[] and fixture.tmux.sent==[]
                    for p in pages: p.close()
                print(f"{width}px pass",flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
