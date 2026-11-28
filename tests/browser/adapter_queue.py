"""Click through inbox to a particular Claude callback, then answer the remaining question."""
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from claude_chat import Owner
from tests.test_adapter_queue import permission
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser = pw.chromium.launch()
            for i, width in enumerate((1440,1180,1024,900,834,768,390), 1):
                pane = dict(fixture.tmux.panes[0], id="%"+str(i), provider="claude", command="claude", pane_pid="222",
                            capabilities={"native_form": "owner"})
                fixture.tmux.panes = [pane]
                owner, writes = Owner(), []
                owner.receive(permission("q", "AskUserQuestion", {"questions": [{"question": "模型", "multiSelect": True,
                                  "options": [{"label": "蓝"}, {"label": "绿"}]}]}), writes.append)
                owner.receive(permission("p", "ExitPlanMode", {"plan": "调整界面 " * 40}), writes.append)
                def route(r):
                    path = urlsplit(r.request.url).path
                    if path == "/" or path.startswith("/assets/") or path.endswith(("/native-form", "/needs-you/inbox")):
                        res = client.post(path, json=r.request.post_data_json) if r.request.method == "POST" else client.get(path)
                        return r.fulfill(status=res.status_code, body=res.content, content_type=res.headers["content-type"])
                    if path == "/api/hosts":
                        return r.fulfill(json={"hosts":[{"id":"localhost", "panes":[pane]}]})
                    if path.endswith("/agents"):
                        return r.fulfill(json={"structured_chat":["claude"], "agents":[{"name":"claude","label":"Claude Code"}]})
                    if path.endswith("/transcript"):
                        return r.fulfill(json={"provider":"claude", "messages":[{"id":"1","role":"user","text":"fixture"}]})
                    return r.fulfill(json={"panes":[], "items":[], "prefs":{}})
                def query(pid, actual, action, provider):
                    assert (pid, actual, provider) == ("222", pane["id"], "claude")
                    return owner.handle(action, writes.append)
                with patch("chat_ipc.query", side_effect=query):
                    page = browser.new_page(viewport={"width":width,"height":844}, has_touch=width==390, service_workers="block")
                    page.route("**/*", route)
                    page.route_web_socket("**/*", lambda ws: None)
                    page.goto("http://tmuxweb.test/?pane=%25"+str(i), wait_until="domcontentloaded")
                    card = page.locator("#native-form")
                    expect(card.get_by_label("选择待处理请求")).to_be_visible()
                    page.locator('[data-new-session="localhost"]').first.evaluate("e=>e.click()")
                    dialog = page.locator("#session-dialog")
                    dialog.locator("#session-agent").select_option("claude")
                    mode = dialog.locator("#session-kimi-chat")
                    expect(mode).to_have_value("terminal")
                    mode.select_option("acp")
                    expect(dialog.locator("#session-args-field")).to_be_hidden()
                    dialog.locator("#session-cancel").click()
                    page.evaluate("TmuxNeedsYouInbox.open()")
                    row = page.locator("#inbox-list button").filter(has_text="ExitPlanMode").last
                    expect(row).to_be_visible()
                    assert row.evaluate("e=>e.scrollWidth<=e.clientWidth+1 && e.getBoundingClientRect().right<=innerWidth+1")
                    row.tap() if width == 390 else row.click()
                    expect(card.locator("legend")).to_have_text("ExitPlanMode")
                    card.get_by_label("拒绝", exact=True).check()
                    assert not writes
                    card.get_by_role("button",name="提交审批决定").click()
                    expect(card.locator("legend")).to_have_text("模型")
                    for label in ("蓝", "绿"):
                        control = card.get_by_label(label, exact=True)
                        control.focus()
                        control.press("Space")
                    assert len(writes) == 1
                    assert card.evaluate("e=>[e,...e.querySelectorAll('button,label,select,legend')].every(x=>x.scrollWidth<=x.clientWidth+1)")
                    card.get_by_role("button",name="提交全部答案").click()
                    expect(card.get_by_role("status")).to_have_text("已回答")
                    assert writes[-1]["response"]["response"]["updatedInput"]["answers"] == {"模型":"蓝, 绿"}
                    page.close()
                print(f"{width}px pass", flush=True)
            assert not fixture.tmux.keys and not fixture.tmux.sent
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
