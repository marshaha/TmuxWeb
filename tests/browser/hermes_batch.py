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


def exercise(page, base_url, pane):
    """Use the normal console entry point and submit only to the designated fixture."""
    choices = []
    page.on("request", lambda r: choices.append(r.post_data_json)
            if r.method == "POST" and r.url.endswith("/prompt/choose") else None)
    page.goto(base_url + "/?" + urlencode({"pane": pane}), wait_until="domcontentloaded")
    inspect = page.locator("#inspect-interaction")
    inspect.click()
    card = page.locator("#prompt-card")
    title = card.locator("#prompt-card-title")
    expect(title).to_have_text("选择 provider？")
    for key, expected in [("Tab", "选择模型？"), ("ArrowLeft", "选择 provider？"),
                          ("ArrowRight", "选择模型？"), ("Shift+Tab", "选择 provider？")]:
        page.keyboard.press(key)
        expect(title).to_be_visible()
        expect(title).to_have_text(expected)
    option = card.get_by_role("option").filter(has_text="claude")
    page.keyboard.press("ArrowDown")
    expect(option).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowUp")
    expect(option).to_have_attribute("aria-selected", "false")

    # A still-open iframe used to cover the visible answer buttons after inspection.
    card.get_by_role("button", name="实时作答").click()
    expect(page.locator("#chat-reply-panel iframe")).to_be_visible()
    inspect.click()
    expect(page.locator("#chat-reply-panel")).to_be_hidden()
    option.click()
    default_model = card.get_by_role("option").filter(has_text="默认模型")
    expect(default_model).to_be_focused()
    page.keyboard.press("Enter")
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
        for name in ("SHARE", "QUEUE", "APPROVALS", "EVENTS", "CALLS", "HOME", "PASSWORD"):
            env["TMUX_WEB_" + name + "_DB"] = str(root / (name.lower() + ".db"))
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
                    page = browser.new_page(viewport={"width": width, "height": 844}, service_workers="block")
                    auth = page.request.post(url + "/login", form={"password": env["TMUX_WEB_PASSWORD"]}, max_redirects=0)
                    assert auth.status == 303
                    exercise(page, url, pane)
                    for _ in range(50):
                        if result.exists():
                            break
                        time.sleep(.1)
                    assert json.loads(result.read_text()) == {"provider": "claude", "model": "默认模型"}
                    page.close()
                    print(f"{width}px: mouse and keyboard answers reached the test PTY", flush=True)
                finally:
                    subprocess.run(["tmux", "kill-session", "-t", f"=test-{width}"], env=env,
                                   stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
