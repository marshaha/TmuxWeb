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
                        sends.append(path)
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
                expect(page.locator("#selected-number")).to_contain_text("2")
                field.fill("@")
                field.press("ArrowUp")
                expect(selected).to_have_attribute("data-complete-index", "7")
                expect(selected).to_be_in_viewport()
                field.press("Tab")
                expect(page.locator("#selected-number")).to_contain_text("8")
                field.fill("@")
                field.press("Enter")
                expect(field).to_have_value("draft ")
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
                    field.fill(f"@#{number}")
                    field.press("Enter")
                    expect(page.locator("#selected-number")).to_contain_text(str(number))
                    field.fill("/panel")
                    expect(selected).to_contain_text("Skill · " + invocation)
                    field.press("Tab")
                    expect(field).to_have_value(invocation + " ")
                assert not sends, sends
                page.close()
                print(f"{width}px pass", flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
