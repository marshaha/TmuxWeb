"""Recognize a wrapped approval without a hook and open Codex in the inline terminal."""
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests
from plugins.agent.hooks.observe import observation


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            cmd = "bash /tmp/release/rollout.sh"
            fixture.tmux.panes[0].update(provider="codex", command="codex", pane_pid="1234")
            approval = (
                f"$ {cmd}\n› 1. Yes, proceed (y)\n"
                f"  2. Yes, and don't ask again for commands that start with `{cmd}`\n"
                "     (p)\n  3. No, and tell Codex what to do differently (esc)\n"
                "  Press enter to confirm or esc to cancel"
            )
            fixture.tmux.capture_ansi = lambda *a, **kw: "\x1b[32m" + fixture.tmux.output + "\x1b[0m"
            record_key = fixture.tmux.send_key
            def move_cursor(pane_id, key, pane=None):
                record_key(pane_id, key, pane=pane)
                if key == "Down":
                    fixture.tmux.output = approval.replace("› 1.", "  1.").replace("  2.", "› 2.")
            fixture.tmux.send_key = move_cursor
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                fixture.tmux.output = approval
                fixture.tmux.keys.clear()
                notices = []
                fixture.tmux.needs_input_many = lambda panes, timeout: {"%4": {"notices": notices}}
                page = browser.new_page(viewport={"width": width, "height": 844}, has_touch=width == 390,
                                        service_workers="block")
                submissions = []
                manual_enabled = False

                def route(r):
                    url = urlsplit(r.request.url)
                    path = unquote(url.path)
                    if path == "/api/prefs":
                        return r.fulfill(json={"prefs": {"manual_inspect": "on" if manual_enabled else "off"}})
                    if path.endswith("/key"):
                        result = client.post(url.path, json=r.request.post_data_json)
                        return r.fulfill(status=result.status_code, json=result.json())
                    if path.endswith("/choose"):
                        submissions.append(r.request.post_data_json)
                        result = client.post(url.path, json=r.request.post_data_json)
                        return r.fulfill(status=result.status_code, json=result.json())
                    if path in ("/", "/api/hosts") or path.startswith("/assets/") or path.endswith(("/prompt", "/tail")):
                        response = client.get(url.path, params=url.query)
                        return r.fulfill(status=response.status_code, body=response.content,
                                         content_type=response.headers.get("content-type", "application/octet-stream"))
                    return r.fulfill(json={"panes": [], "messages": [], "text": "", "items": [], "prefs": {}})

                page.route("**/*", route)
                page.route_web_socket("**/*", lambda ws: ws.close())
                page.goto("http://tmuxweb.test/?pane=%254", wait_until="domcontentloaded")
                inspect = page.locator("#inspect-interaction")
                fixture.tmux.output = ("Would you like to run the following command?\n"
                                       "Environment: local\nReason: 发布已测试的版本\n" + approval)
                card = page.locator("#interaction-inline #prompt-card")
                expect(card.get_by_text("Codex 等待命令审批")).to_be_visible(timeout=16000)
                expect(card).to_contain_text(cmd)
                expect(card.get_by_role("button",name="确认当前选项")).to_be_hidden()
                expect(inspect).to_be_hidden()
                assert not submissions and not fixture.tmux.keys
                fixture.tmux.output = "› Ask Codex to do anything"
                expect(card).to_be_hidden(timeout=12000)
                manual_enabled = True
                fixture.tmux.output = approval
                page.reload(wait_until="domcontentloaded")
                expect(inspect).to_have_text("查看命令审批", timeout=16000)
                assert inspect.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                live_keys = page.locator("#live-keys-nav")
                expect(live_keys).to_be_hidden()
                page.locator("#live-keyboard-toggle").click()
                expect(live_keys).to_be_visible()
                live_keys.get_by_role("button", name="向下").click()
                expect(page.locator("#live-tail-text")).to_contain_text("› 2.")
                with page.expect_response(lambda r: r.url.endswith("/key")):
                    live_keys.get_by_role("button", name="Enter").click()
                assert not submissions and fixture.tmux.keys == [("%4", "Down"), ("%4", "Enter")]
                assert not page.locator("#live-tail iframe").count()
                fixture.tmux.keys.clear()
                fixture.tmux.output = approval
                inspect.click()
                card = page.locator("#interaction-inline #prompt-card")
                expect(card.locator("#prompt-card-title")).to_have_text("Codex 命令审批")
                options = card.get_by_role("option")
                expect(options).to_have_count(3)
                expect(options.nth(1)).to_contain_text(cmd)
                for option in options.all():
                    expect(option).to_be_enabled()
                    assert option.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                expect(card.locator("#prompt-card-nav")).to_be_hidden()
                first = options.nth(0)
                fixture.tmux.output = "› Ask Codex to do anything"
                expect(card).to_be_hidden(timeout=12000)
                assert not submissions and not fixture.tmux.keys
                fixture.tmux.output = approval
                expect(inspect).to_have_text("查看命令审批", timeout=12000)
                inspect.click()
                expect(options.nth(1)).to_contain_text("实时确认")
                if width == 390:
                    options.nth(1).tap()
                else:
                    options.nth(1).click()
                panel = page.locator("#interaction-inline #chat-reply-panel")
                expect(panel).to_be_hidden()
                assert not submissions and not fixture.tmux.keys
                card.get_by_role("button",name="确认当前选项").click()
                expect(panel.locator("iframe")).to_be_visible()
                expect(panel).to_contain_text("批准并记住命令前缀")
                assert not submissions and not fixture.tmux.keys
                panel.get_by_role("button", name="收起").click()
                session, request, _, title, detail = observation("codex", "PermissionRequest", {
                    "session_id": "session", "turn_id": f"test-{width}", "tool_name": "Bash",
                    "tool_input": {"command": cmd}})
                notices.append({"id": [session, request], "title": title, "detail": detail})
                # A new native request may arrive inside the manual preview cache window.
                page.wait_for_timeout(1300)
                inspect.click()
                expect(page.locator("#prompt-card-hint")).to_contain_text("选好后点「确认选择」")
                if width == 390:
                    nav = card.locator("#prompt-card-nav")
                    expect(nav.get_by_role("button", name="下一题")).to_be_hidden()
                    nav.get_by_role("button", name="下一项").tap()
                    expect(options.nth(1)).to_have_attribute("aria-selected", "true")
                    nav.get_by_role("button", name="上一项").tap()
                    assert not submissions and not fixture.tmux.keys
                    card.get_by_role("button", name="确认当前选项").tap()
                else:
                    options.last.click()
                    first.click()
                    first.press("Enter")
                    assert not submissions and not fixture.tmux.keys
                    card.get_by_role("button",name="确认当前选项").press("Enter")
                expect(card).to_be_hidden()
                expect(page.locator("#toast")).to_have_text("已发送「仅批准这一次」")
                assert len(submissions) == 1 and submissions[0]["index"] == 0
                assert fixture.tmux.keys == [("%4", "y")] and not fixture.tmux.sent
                page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
