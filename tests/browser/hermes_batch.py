"""Run with python tests/browser/hermes_batch.py after installing Playwright Chromium and tmux."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def exercise(page, base_url, pane, result):
    """Use the normal console entry point and submit only to the designated fixture."""
    expect.set_options(timeout=20000)
    choices = []
    page.on("request", lambda r: choices.append(r.post_data_json)
            if r.method == "POST" and r.url.endswith("/prompt/choose") else None)
    # Exercise the second card source via the normal shared status endpoint.
    status_route = "**/api/panes/status"
    notice = {"kind": "notice", "title": "等待审批", "read_only": True,
              "revision": "notice", "options": [{"label": "核对当前请求"}]}
    page.route(status_route, lambda route: route.fulfill(json={
        "panes": [{"id": pane, "needs_input": True, "prompt": notice}]}))
    messages = [{"id": str(i), "role": "assistant" if i % 2 else "user",
                 "text": f"Fixture message {i}", "timestamp": 1700000000 + i} for i in range(500)]
    def transcript(route):
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("since") == [messages[-1]["id"]]:
            route.fulfill(json={"unchanged": True, "has_more": False, "mtime": 1})
            return
        limit = int(query.get("limit", [300])[0])
        route.fulfill(json={"provider": "hermes", "messages": messages[-limit:],
                            "mtime": 1, "has_more": limit < len(messages)})
    page.route("**/transcript?*", transcript)
    page.goto(base_url + "/?" + urlencode({"pane": pane}), wait_until="domcontentloaded")
    inspect = page.locator("#inspect-interaction")
    placement = page.get_by_role("combobox", name="交互卡片显示方式")
    expect(placement).to_have_value("inline")
    expect(page.locator("#interaction-inline #prompt-card-title")).to_have_text("等待审批")
    notice_option = page.locator("#interaction-inline #prompt-card").get_by_role("option")
    expect(notice_option).to_be_visible()
    expect(notice_option).to_be_disabled()
    expect(page.get_by_role("group", name="交互按键")).to_be_hidden()
    expect(inspect).to_be_visible()
    placement.select_option("manual")
    expect(page.locator("#prompt-card")).to_be_hidden()
    page.reload(wait_until="domcontentloaded")
    expect(placement).to_have_value("manual")
    expect(inspect).to_be_enabled()
    expect(page.locator("#prompt-card")).to_be_hidden()
    page.unroute(status_route)
    inspect.click()
    card = page.locator("#prompt-card")
    title = card.locator("#prompt-card-title")
    expect(title).to_have_text("暂无已确认的交互")
    expect(card.locator("#prompt-card-raw")).to_contain_text("选择 provider？")
    expect(card.get_by_role("option")).to_have_count(0)
    assert not choices
    result.with_suffix(".allow-hook").touch()
    for _ in range(100):
        if result.with_suffix(".ready").exists():
            break
        time.sleep(.05)
    assert result.with_suffix(".ready").exists()
    page.locator("#load-earlier").click()
    expect(page.locator("#transcript-messages .chat-message")).to_have_count(500)
    placement.select_option("inline")
    # Manual display reads are briefly shared; an action always re-reads the hook.
    for _ in range(10):
        inspect.click()
        if title.text_content() == "选择 provider？":
            break
        page.wait_for_timeout(250)
    expect(title).to_have_text("选择 provider？")
    inline_card = page.locator("#scroll #interaction-inline #prompt-card")
    expect(inline_card).to_be_visible()
    expect(inline_card).to_have_role("region")
    # Changing placement while the form is open must preserve its live controls.
    card.evaluate("node => window.originalCard = node")
    placement.select_option("popup")
    expect(page.locator(".composer #prompt-card")).to_be_visible()
    expect(card).to_have_role("dialog")
    placement.select_option("inline")
    assert inline_card.evaluate("node => node === window.originalCard")
    assert not choices
    inspect.click()
    expect(card.get_by_role("option").first).to_be_focused()
    assert page.locator("*").count() <= 12000
    assert placement.evaluate("el => el.clientWidth >= el.scrollWidth")
    assert inline_card.locator("button:visible,.prompt-title,.prompt-label").evaluate_all("""els => els.every(el => {
        const range = document.createRange();
        range.selectNodeContents(el);
        const text = range.getBoundingClientRect(), rect = el.getBoundingClientRect();
        return !text.width || (text.left >= rect.left - 1 && text.right <= rect.right + 1
            && text.top >= rect.top - 1 && text.bottom <= rect.bottom + 1);
    })""")
    for key, expected in [("Tab", "选择模型？"), ("ArrowLeft", "选择 provider？"),
                          ("ArrowRight", "选择模型？"), ("ArrowLeft", "选择 provider？")]:
        if key in ("Tab", "ArrowLeft"):
            button = card.get_by_role("button", name="下一题" if key == "Tab" else "上一题")
            if page.viewport_size["width"] == 390:
                button.tap()
            else:
                button.click()
        else:
            page.keyboard.press(key)
        expect(title).to_have_text(expected, timeout=45000)
        expect(card.get_by_role("option", selected=True)).to_be_focused()
    option = card.get_by_role("option").filter(has_text="claude")
    card.get_by_role("button", name="下一项").click()
    expect(option).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowUp")
    expect(option).to_have_attribute("aria-selected", "false")

    # A still-open iframe used to cover the visible answer buttons after inspection.
    card.get_by_role("button", name="实时作答").click()
    expect(page.locator("#interaction-inline #chat-reply-panel iframe")).to_be_visible()
    inspect.click()
    expect(page.locator("#chat-reply-panel")).to_be_hidden()
    option.click()
    expect(option).to_have_attribute("aria-selected", "true")
    assert len(choices) == 4
    card.get_by_role("button", name="确认当前选项").click()
    default_model = card.get_by_role("option").filter(has_text="默认模型")
    expect(default_model).to_be_focused(timeout=45000)
    card.get_by_role("button", name="确认当前选项").click()
    expect(card).to_be_hidden()
    assert [r.get("key", r.get("index")) for r in choices] == ["Tab", "BTab", "Tab", "BTab", 1, 0]
    assert all(r["revision"] for r in choices)


def main():
    with tempfile.TemporaryDirectory(prefix="tw-chat-") as directory, ExitStack() as stack:
        root = Path(directory)
        env = {**os.environ, "HOME": directory, "TMUX_TMPDIR": directory,
               "TMUX_WEB_PASSWORD": secrets.token_urlsafe(24)}
        for key in ("TMUX", "TMUX_PANE"):
            env.pop(key, None)
        for name in ("SHARE", "QUEUE", "APPROVALS", "EVENTS", "CALLS", "HOME", "PASSWORD", "HOST", "FED"):
            env["TMUX_WEB_" + name + "_DB"] = str(root / (name.lower() + ".db"))
        # Keep pane IDs unique across widths on this private tmux socket.
        subprocess.run(["tmux", "new-session", "-d", "-s", "fixture-anchor", "sleep 3600"], env=env, check=True)
        stack.callback(subprocess.run, ["tmux", "kill-session", "-t", "=fixture-anchor"], env=env)
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        url = "http://127.0.0.1:" + str(listener.getsockname()[1])
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "tmux_web:create_app",
                                   "--factory", "--fd", str(listener.fileno()), "--log-level", "warning"],
                                  cwd=ROOT, env=env, pass_fds=(listener.fileno(),))
        stack.callback(server.wait, 25)
        stack.callback(server.terminate)
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("test server exited")
            try:
                with urlopen(url + "/login", timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width in (1440, 1180, 1024, 900, 834, 768, 390):
                result = root / f"answer-{width}.json"
                pane = subprocess.check_output(["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}",
                                                "-s", f"test-{width}", "-x", "120", "-y", "36", "-c", directory,
                                                sys.executable, str(Path(__file__).with_name("hermes_fixture.py").resolve()),
                                                str(result)], env=env, text=True).strip()
                try:
                    page = browser.new_page(viewport={"width": width, "height": 844}, has_touch=width == 390, service_workers="block")
                    auth = page.request.post(url + "/login", form={"password": env["TMUX_WEB_PASSWORD"]}, max_redirects=0)
                    assert auth.status == 303
                    assert page.request.put(url + "/api/prefs", data={"manual_inspect": "on"}).ok
                    hosts = page.request.get(url + "/api/hosts?refresh=1&host=localhost").json()["hosts"]
                    assert any(p["id"] == pane for h in hosts for p in h["panes"])
                    exercise(page, url, pane, result)
                    for _ in range(50):
                        if result.exists():
                            break
                        time.sleep(.1)
                    assert json.loads(result.read_text()) == {"provider": "claude", "model": "默认模型"}
                    page.close()
                    print(f"{width}px pass", flush=True)
                finally:
                    subprocess.run(["tmux", "kill-session", "-t", f"=test-{width}"], env=env,
                                   stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
