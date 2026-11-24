"""Exercise whole keyboard events in the composer with fake panes."""
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture = TmuxWebTests()
    fixture.setUp()
    try:
        with TemporaryDirectory() as temp, patch.dict(os.environ), fixture.client as client, sync_playwright() as pw:
            providers = ["codex", "claude", "hermes", "kimi"]
            for provider, variable in zip(providers, ["CODEX_HOME", "CLAUDE_CONFIG_DIR", "HERMES_HOME", "KIMI_CODE_HOME"]):
                os.environ[variable] = str(Path(temp) / provider)
                manifest = Path(os.environ[variable]) / "skills" / "panel-review" / "SKILL.md"
                manifest.parent.mkdir(parents=True)
                manifest.write_text("---\nname: panel-review\ndescription: Review the layout\n---\nprivate body")
            fixture.login()
            panes = [dict(fixture.tmux.panes[0], id=f"%{i}", number=i, provider=providers[(i-1)%4],
                          title=f"pane-{i}-" + "x" * 90) for i in range(1, 9)]
            panes[7]['prefix'] = 'robo'
            fixture.tmux.panes = panes
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                page = browser.new_page(viewport={"width": width, "height": 700},
                                        has_touch=width == 390, service_workers="block")
                sends = []

                def route(r):
                    path = urlsplit(r.request.url).path
                    if path == "/api/hosts":
                        return r.fulfill(json={"hosts": [{"id": "localhost", "panes": panes}]})
                    if path == "/" or path.startswith("/assets/") or path.endswith("/skills"):
                        res = client.get(path)
                        return r.fulfill(body=res.content, content_type=res.headers["content-type"])
                    if path.endswith(("/input", "/keys", "/queue")) and r.request.method == "POST":
                        sends.append((path, r.request.post_data_json))
                    return r.fulfill(json={"panes": [], "messages": [], "items": [], "prefs": {}})

                page.route("**/*", route)
                page.route_web_socket("**/*", lambda ws: ws.close())
                page.goto("http://tmuxweb.test/?pane=%251", wait_until="domcontentloaded")
                expect(page.locator("#selected-number")).to_contain_text("1")
                field = page.locator("#input")
                selected = page.locator('#input-complete [aria-selected="true"]')
                field.fill("draft @")
                for key, index in (("ArrowDown", 1), ("ArrowDown", 2), ("ArrowUp", 1)):
                    field.press(key)
                    expect(selected).to_have_attribute("data-complete-index", str(index))
                assert selected.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                field.press("Enter")
                expect(field).to_have_value("draft @2 ")
                expect(page.locator("#selected-number")).to_contain_text("1")
                field.fill("ask @")
                field.press("ArrowUp")
                expect(selected).to_have_attribute("data-complete-index", "7")
                if width == 390:
                    selected.tap()
                else:
                    field.press("Tab")
                expect(field).to_have_value("ask @robo#8 ")
                expect(page.locator("#selected-number")).to_contain_text("1")
                field.fill("/")
                skill = page.get_by_role("option", name="panel-review Skill · $panel-review")
                expect(skill).to_be_visible()
                field.fill("/panel-rev")
                expect(selected).to_contain_text("panel-review")
                if width == 390:
                    skill.tap()
                else:
                    field.press("Enter")
                expect(field).to_have_value("$panel-review ")
                expect(page.locator("#input-complete")).to_be_hidden()
                field.fill("/z")
                expect(selected).to_contain_text("总结")
                field.press("Tab")
                expect(field).to_have_value("总结一下目前的进展：已完成什么、正在做什么、还剩什么")
                field.fill("@")
                field.press("Escape")
                expect(page.locator("#input-complete")).to_be_hidden()
                for number, invocation in ((2, "/panel-review"), (3, "/skill panel-review"), (4, "/skill:panel-review")):
                    page.goto(f"http://tmuxweb.test/?pane=%25{number}", wait_until="domcontentloaded")
                    expect(page.locator("#selected-number")).to_contain_text(str(number))
                    field.fill("/panel")
                    expect(selected).to_contain_text("Skill · " + invocation)
                    field.press("Tab")
                    expect(field).to_have_value(invocation + " ")
                # The Kimi catalog survives a page reload and is usable before the HTTP reply.
                pending = []
                page.route("**/skills", lambda route: pending.append(route))
                page.reload(wait_until="domcontentloaded")
                field.fill("/")
                cached = page.get_by_role("option", name="panel-review Skill · /skill:panel-review")
                expect(cached).to_be_visible()
                expect(page.locator("#input-complete")).not_to_contain_text("正在读取")
                page.wait_for_timeout(50)
                assert len(pending) == 1
                pending.pop().fulfill(json={"provider":"kimi","skills":["updated-skill"]})
                expect(cached).to_have_count(0)
                updated = page.get_by_role("option", name="updated-skill Skill · /skill:updated-skill")
                expect(updated).to_be_visible()
                page.reload(wait_until="domcontentloaded")
                field.fill("/updated")
                expect(updated).to_be_visible()
                page.wait_for_timeout(50)
                pending.pop().fulfill(status=503, json={})
                expect(page.locator("#input-complete")).to_contain_text("显示已缓存技能")
                field.press("Tab")
                expect(field).to_have_value("/skill:updated-skill ")
                assert not sends, sends
                page.goto("http://tmuxweb.test/?pane=%255", wait_until="domcontentloaded")
                field.fill("/updated")
                expect(page.locator("#input-complete")).to_contain_text("正在读取技能")
                expect(updated).to_have_count(0)
                field.fill("请和 @#2")
                field.press("Tab")
                expect(field).to_have_value("请和 @2 ")
                field.press("End")
                field.press("x")
                if width == 390:
                    page.get_by_role("button", name="发送", exact=True).tap()
                else:
                    field.press("Enter")
                expect(field).to_have_value("")
                assert len(sends) == 1 and sends[0][0].endswith("/%255/input"), sends
                assert sends[0][1]["text"] == "请和 @2 x", sends
                if pending:
                    pending.pop().fulfill(json={"provider":"codex","skills":[]})
                page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
