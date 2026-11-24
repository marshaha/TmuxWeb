"""Browser check for the owner's controls and the isolated guest entry point."""
from pathlib import Path
import sys
from contextlib import contextmanager
import socket
import threading
import time

import uvicorn

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.test_tmux_web import TmuxWebTests


def fits(page, selector):
    assert page.locator(selector).evaluate_all("""els=>els.every(el=>!el.offsetWidth ||
        (el.scrollWidth<=el.clientWidth+1 && el.scrollHeight<=el.clientHeight+1))"""), selector
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@contextmanager
def fixture_server(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        server=uvicorn.Server(uvicorn.Config(app,log_level="error",lifespan="off"))
        thread=threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:break
                time.sleep(.025)
            assert server.started
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit=True
            thread.join(10)


def main():
    setup=TmuxWebTests()
    setup.setUp()
    setup.tmux.panes[0].update(command="claude",provider="claude")
    pane=dict(setup.tmux.panes[0],number=4,capabilities={"share":True})
    messages=[{"id":str(n),"role":"assistant","text": "| A | B |\n|---|---|\n" + "| test | cell |\n"*30}
              for n in range(500)]
    setup.tmux.transcript=lambda pane_id,limit=300,refresh=False,pane=None: {"session_id":"s1","provider":"claude","mtime":1,"messages":messages[-limit:]}
    with fixture_server(setup.client.app) as base, sync_playwright() as pw:
        expect.set_options(timeout=20000)
        browser=pw.chromium.launch()
        for width in [1440,1180,1024,900,834,768,390]:
            slug=f"layout-{width}"
            owner=browser.new_page(viewport={"width":width,"height":844},service_workers="block")
            visitor=browser.new_page(viewport={"width":width,"height":844},service_workers="block")
            owner.request.post(base+"/login",form={"password":"unit-test-admin-password"})
            owner.route("**/api/hosts",lambda r:r.fulfill(json={"hosts":[{"id":"localhost","name":"localhost","panes":[pane]}]}))
            owner.goto(base+"/?pane=%254",wait_until="domcontentloaded")
            owner.locator("#share-button").click()
            expect(owner.locator("#share-allow-model")).not_to_be_checked()
            expect(owner.locator("#share-allow-terminal")).not_to_be_checked()
            owner.locator("#share-slug").fill(slug)
            owner.locator("#share-allow-model").check()
            owner.locator("#share-allow-terminal").check()
            fits(owner,"#share-dialog button,#share-dialog label")
            owner.locator('#share-form button[type="submit"]').click()
            expect(owner.locator("#share-result")).to_contain_text(slug)
            visitor.goto(base+"/share/"+slug,wait_until="domcontentloaded")
            expect(visitor.locator("#messages article")).to_have_count(500)
            assert visitor.evaluate("document.querySelectorAll('*').length")<=12000
            visitor.locator("#choose-model").click()
            visitor.locator("#model-options").select_option(index=1)
            expect(visitor.locator("#share-note")).to_contain_text("已发送模型切换命令")
            visitor.locator("#terminal-mode").click()
            expect(visitor.locator("#terminal-screen")).to_contain_text("hello")
            visitor.locator("#terminal-keys").get_by_role("button",name="↓").click()
            visitor.locator("#terminal-screen").press("Enter")
            visitor.locator("#input").fill("sample")
            visitor.locator("#send").click()
            visitor.wait_for_function("!document.querySelector('#send').disabled")
            assert setup.tmux.keys[-2:]==[("%4","Down"),("%4","Enter")]
            assert setup.tmux.sent[-1]==("%4","sample",True)
            fits(visitor,".tools button,.terminal-keys button")
            assert visitor.locator("#terminal-keys button").evaluate_all(
                "buttons => new Set(buttons.map(b => b.offsetTop)).size === 1")
            owner.goto(base+"/shares",wait_until="domcontentloaded")
            row=owner.locator(".card").filter(has_text=slug)
            row.get_by_role("button",name="权限").click()
            expect(owner.locator("#perm-terminal")).to_be_checked()
            owner.locator("#perm-terminal").uncheck()
            owner.locator("#perm-input").uncheck()
            fits(owner,"#permissions-dialog label,#permissions-dialog button")
            owner.locator("#permissions-form").get_by_role("button",name="保存").click()
            expect(owner.locator("#permissions-dialog")).not_to_be_visible()
            expect(visitor.locator("#terminal-mode")).to_be_hidden(timeout=15000)
            expect(visitor.locator("#choose-model")).to_be_visible()
            expect(visitor.locator("#messages article")).to_have_count(500)
            visitor.close()
            owner.close()
            print(width,"px pass",flush=True)
        browser.close()

if __name__=="__main__":
    main()
