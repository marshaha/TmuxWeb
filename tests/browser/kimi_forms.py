"""Exercise two clients against the Kimi adapter without a model or production pane."""
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import kimi_acp
from tests.test_kimi_acp import QUESTIONS
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser=pw.chromium.launch()
            for width in (1440,1180,1024,900,834,768,390):
                pane=dict(fixture.tmux.panes[0],provider="kimi",command="kimi",pane_pid="123",
                          capabilities={"native_form":"owner"})
                fixture.tmux.panes=[pane]
                owner=kimi_acp.Owner()
                owner.session=QUESTIONS["sessionId"]
                owner.register(17,"elicitation/create",QUESTIONS)
                writes=[]
                def query(pid, actual, action):
                    assert (pid,actual)==("123","%4")
                    return owner.handle(action,writes.append)
                def route(r):
                    path=urlsplit(r.request.url).path
                    if path=="/" or path.startswith("/assets/") or path.endswith("/native-form"):
                        res=(client.post(path,json=r.request.post_data_json) if r.request.method=="POST" else client.get(path))
                        return r.fulfill(status=res.status_code,body=res.content,content_type=res.headers["content-type"])
                    if path.endswith("/agents"):
                        return r.fulfill(json={"kimi_chat":True,"agents":[{"name":"kimi","label":"Kimi","path":"/fixture/kimi"}]})
                    if path=="/api/hosts":return r.fulfill(json={"hosts":[{"id":"localhost","panes":[pane]}]})
                    if path.endswith("/transcript"):
                        return r.fulfill(json={"provider":"kimi","messages":[{"id":"1","role":"user","text":"fixture"}]})
                    return r.fulfill(json={"panes":[],"items":[],"messages":[],"prefs":{}})
                with patch("kimi_acp.query",side_effect=query):
                    pages=[]
                    for _ in range(2):
                        page=browser.new_page(viewport={"width":width,"height":844},has_touch=width==390,service_workers="block")
                        pages.append(page); page.route("**/*",route)
                        page.route_web_socket("**/*",lambda ws:None)
                        page.goto("http://tmuxweb.test/?pane=%254",wait_until="domcontentloaded")
                        card=page.locator("#native-form")
                        expect(card).to_contain_text("Kimi 等你回答")
                        if len(pages)==1:
                            page.locator('[data-new-session="localhost"]').first.evaluate("e=>e.click()")
                            dialog=page.locator("#session-dialog")
                            expect(dialog).to_be_visible()
                            dialog.locator("#session-agent").select_option("kimi")
                            switch=dialog.locator("#session-kimi-chat")
                            expect(switch).to_be_visible()
                            expect(switch).to_have_value("terminal")
                            switch.select_option("acp")
                            expect(dialog.locator("#session-args-field")).to_be_hidden()
                            assert switch.evaluate("e=>e.getBoundingClientRect().right<=innerWidth")
                            dialog.locator("#session-cancel").click()
                        for name in ("One","Two","m1","m2"):
                            control=card.get_by_label(name,exact=True)
                            control.tap() if width==390 else control.click()
                        assert card.evaluate("el=>[el,...el.querySelectorAll('button,label,legend')].every(x=>x.scrollWidth<=x.clientWidth+1)")
                    assert writes==[]
                    for i,page in enumerate(pages):
                        card=page.locator("#native-form")
                        if i: card.get_by_label("One",exact=True).check()
                        card.get_by_role("button",name="提交全部答案").click()
                        expect(card.get_by_role("status")).to_contain_text("已结束" if i else "已回答")
                    assert writes==[{"jsonrpc":"2.0","id":17,"result":{"action":"accept","content":{"q0":"p2","q1":["m1","m2"]}}}]
                    for page in pages: page.close()
                assert fixture.tmux.keys==[] and fixture.tmux.sent==[]
                print(f"{width}px pass",flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__=="__main__":
    main()
