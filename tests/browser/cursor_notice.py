"""Cursor evidence appears and clears without sending any pane input."""
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_cursor_notice import ROWS, frame
import federation
from tests.test_federation import federation_app, make_client, memory_stores
from tests.test_tmux_web import FakeTmux


def main():
    backend = FakeTmux()
    backend.panes[0].update(command="kimi", provider="kimi", pane_pid="123")
    backend.output = "\n".join(ROWS)
    backend.capture_ansi = lambda *args, **kwargs: backend.output
    child_store, grants = memory_stores()
    child = TestClient(federation_app(backend, child_store, grants))
    token = grants.issue("parent", ["view"], ["%4"], 600)["token"]
    client, nodes, _ = make_client()
    nodes.add("child", "child", "http://fixture.invalid", "", token)
    node = nodes.listing()[0]["id"]

    def child_get(peer, path, timeout=None):
        response = child.get(path, headers={"Authorization": "Bearer " + peer.token})
        response.raise_for_status()
        return response.json()

    with patch.object(federation.FedClient, "_get", child_get), sync_playwright() as pw:
        client.post("/login", data={"password": "unit-test-admin-password"})
        browser = pw.chromium.launch()
        for width in (1440, 1180, 1024, 900, 834, 768, 390):
            cursor = {"y": 1}
            def snapshot(actual):
                rows = list(ROWS)
                if cursor["y"] == 2:
                    rows[1:3] = ["  1. Local", "› 2. Remote"]
                return frame(rows, y=cursor["y"])
            backend.cursor_frame = snapshot
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    has_touch=width == 390, service_workers="block")

            def route(r):
                url = urlsplit(r.request.url)
                if (url.path in ("/", "/api/hosts") or url.path.startswith("/assets/")
                        or url.path.endswith("/tail") or url.path.startswith("/api/needs-you/inbox")):
                    resp = client.request(r.request.method, url.path, params=url.query)
                    return r.fulfill(status=resp.status_code, body=resp.content,
                                     content_type=resp.headers.get("content-type", "application/octet-stream"))
                return r.fulfill(json={"panes": [], "items": [], "messages": [], "prefs": {}})

            page.route("**/*", route)
            page.route_web_socket("**/*", lambda ws: None)
            page.goto(f"http://tmuxweb.test/?pane={node}~%254", wait_until="domcontentloaded")
            card = page.locator("#scroll article#cursor-chat")
            expect(card.get_by_role("heading")).to_have_text(ROWS[0], timeout=15000)
            expect(card.locator("li")).to_have_count(2)
            expect(card).to_be_visible()
            expect(card.locator("[aria-current]")).to_contain_text("Local")
            card.locator("li").last.click()
            expect(card.locator("[aria-current]")).to_contain_text("Local")
            expect(card.get_by_role("button")).to_have_count(0)
            expect(page.locator("#prompt-card")).to_be_hidden()
            for el in (card, card.get_by_role("heading"), *card.locator("li").all()):
                assert el.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            cursor["y"] = 2
            expect(card.locator("[aria-current]")).to_contain_text("Remote", timeout=12000)
            expect(card.locator("li")).to_have_count(2)
            page.evaluate("TmuxNeedsYouInbox.open()")
            row = page.locator("#inbox-list .inbox-item", has_text=ROWS[0])
            expect(row).to_have_count(1)
            cursor["y"] = 5  # Input history uses the same screen with the caret back in the editor.
            expect(row).to_have_count(0, timeout=16000)
            expect(card).to_be_hidden()
            assert not backend.sent and not backend.keys
            page.close()
            print(f"{width}px pass", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
