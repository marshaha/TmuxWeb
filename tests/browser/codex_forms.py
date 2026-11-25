"""Operate a Codex follow-up entirely in Chat; inputs and answers belong to a fake owner."""
from pathlib import Path
import asyncio
from contextlib import contextmanager
import json
import os
from threading import Thread
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

from websockets.sync.server import unix_serve
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_codex_forms import Owner
from tests.test_tmux_web import TmuxWebTests


@contextmanager
def native_daemon(owner):
    directory = owner.home / ".codex/app-server-control"
    directory.mkdir()
    def handle(ws):
        for raw in ws:
            request = json.loads(raw)
            method = request["method"]
            if method == "initialized":
                continue
            result = {} if method == "initialize" else asyncio.run(owner.rpc(method, request["params"]))
            ws.send(json.dumps({"id":request["id"], "result":result}))
    with unix_serve(handle, path=str(directory / "app-server-control.sock")) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield
        finally:
            server.shutdown()
            worker.join(timeout=3)


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            fixture.tmux.panes[0].update(provider="codex",command="codex",pane_pid="135")
            fixture.tmux.capture_ansi = lambda *args, **kwargs: fixture.tmux.output
            browser = pw.chromium.launch()
            for width in (1440,1180,1024,900,834,768,390):
                with tempfile.TemporaryDirectory(prefix="tw-form-", dir="/tmp") as work:
                    owner = Owner(Path(work))
                    fixture.tmux.panes[0].update(title=owner.name, path="/repo")
                    fixture.tmux.output = owner.screen
                    page = browser.new_page(viewport={"width":width,"height":844},has_touch=width==390,
                                            service_workers="block")
                    def route(r):
                        u = urlsplit(r.request.url)
                        if u.path.endswith("/transcript"):
                            return r.fulfill(json={"provider":"codex","messages":[],"open_questions":[],
                                                   "async_question_record":{"id":"question-id","text":"Which color?\nBlue\nGreen\nOrange"}})
                        if u.path in ("/","/api/hosts") or u.path.startswith("/assets/") or u.path.endswith(("/tail","/native-form")):
                            resp=client.request(r.request.method,u.path,params=u.query,
                                                json=r.request.post_data_json if r.request.method=="POST" else None)
                            return r.fulfill(status=resp.status_code,body=resp.content,
                                             content_type=resp.headers.get("content-type","application/octet-stream"))
                        return r.fulfill(json={"panes":[],"messages":[],"items":[],"prefs":{}})
                    page.route("**/*",route)
                    page.route_web_socket("**/*",lambda ws:None)
                    with native_daemon(owner), patch.dict(os.environ, HOME=str(owner.home)):
                        page.goto("http://tmuxweb.test/?pane=%254",wait_until="domcontentloaded")
                        card=page.locator("#native-form")
                        expect(card.get_by_role("heading",name="Codex 等你回答")).to_be_visible(timeout=18000)
                        footer=page.locator("#interaction-inline")
                        expect(card.get_by_role("radio")).to_have_count(4)
                        expect(card.get_by_text("Which color?",exact=True)).to_have_count(1)
                        expect(footer.locator("#prompt-card")).to_be_hidden()
                        expect(footer).not_to_contain_text("Which color?")
                        first,second=card.get_by_label("Blue",exact=True),card.get_by_label("Green",exact=True)
                        first.tap() if width==390 else first.click()
                        second.tap() if width==390 else second.press("Space")
                        expect(second).to_be_checked()
                        expect(first).not_to_be_checked()
                        page.locator("#native-open-terminal").click()
                        expect(page.locator("#codex-followup-key")).to_be_visible()
                        expect(footer).not_to_contain_text("本轮最近发布的提问")
                        expect(card.get_by_role("radio",checked=True)).to_have_count(1)
                        expect(footer.locator("#prompt-card")).to_be_hidden()
                        assert not owner.sent and not fixture.tmux.keys
                        submit=card.locator("button[type=submit]")
                        assert submit.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
                        submit.tap() if width==390 else submit.click()
                        expect(card).to_contain_text("回答已发送到本轮 Codex 对话")
                        assert len(owner.sent)==1 and owner.sent[0]["input"][0]["text"].endswith("Green")
                        assert not fixture.tmux.keys and not fixture.tmux.sent
                        fixture.tmux.output = "› Ask Codex to do anything"
                        expect(page.locator("#codex-followup-key")).to_be_hidden(timeout=10000)
                        expect(footer.locator("#prompt-card")).to_be_hidden()
                        page.close()
                    print(f"{width}px pass",flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
