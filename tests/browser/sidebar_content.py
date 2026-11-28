"""Exercise sidebar content search with Chromium and the authenticated API on fake panes."""
from pathlib import Path
import sys
import os
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def check_host_rows(page):
    if page.viewport_size["width"] > 760:
        expect(page.locator("#sidebar")).to_have_css("width", "200px")
    rows = page.locator(".tree-host > summary")
    expect(rows).to_have_count(2)
    expect(rows.first).to_be_visible()
    metrics = rows.evaluate_all("""rows => rows.map(row => {
        const rect = selector => row.querySelector(selector).getBoundingClientRect();
        const label = row.querySelector('.tree-label');
        const icon = rect('.host-icon'), dot = rect('.host-status'), add = rect('.host-add');
        const refresh = rect('.host-refresh'), count = rect('.tree-count');
        return {columns: [icon.left, dot.left, rect('.tree-label').left, count.right, add.left, refresh.left],
            sizes: [icon.width, icon.height, dot.width, dot.height],
            clipped: label.scrollWidth > label.clientWidth,
            fits: label.clientWidth > 0 && refresh.right <= row.getBoundingClientRect().right};
    })""")
    assert metrics[0]["columns"] == metrics[1]["columns"], metrics
    assert all(m["sizes"] == [20, 20, 7, 7] and m["fits"] for m in metrics), metrics
    assert metrics[0]["clipped"], metrics


def main():
    fixture = TmuxWebTests()
    with patch.dict(os.environ, {"TMUX_WEB_SEARCH_CACHE_SECONDS":"0"}):
        fixture.setUp()
    marker = "MCPHub <img> " + "x" * 190
    original = fixture.tmux.panes[0]
    panes = [dict(original, id=f"%{i}", pane_pid=str(1000 + i), command="hermes",
                  title="普通会话", provider="hermes", number=369 if i == 442 else i)
             for i in range(420, 443)]
    fixture.tmux.panes = [dict(pane) for pane in panes]

    def transcript(pid, limit, **kwargs):
        texts = [marker if pid == "%442" else "普通消息", *(["稍后的消息"] * 450)]
        return {"messages": [{"text": t} for t in texts[-limit:]]}

    fixture.tmux.transcript = transcript
    with fixture.client as client, sync_playwright() as pw:
        fixture.login()
        client.get("/api/hosts")
        browser = pw.chromium.launch()
        for width in (1440, 1180, 1024, 900, 834, 768, 390):
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    has_touch=width == 390, service_workers="block")
            page.add_init_script("localStorage.setItem('tmux-side-w', '200')")
            batches = []
            cache_checks = []
            fail_one_batch = False

            def route(r):
                nonlocal fail_one_batch
                path = urlsplit(r.request.url).path
                if path == "/api/hosts":
                    return r.fulfill(json={"hosts": [
                        {"id": "localhost", "name": "Local-" + "long-host-name-" * 5, "panes": panes},
                        {"id": "nas", "name": "nas", "panes": [], "status":"online", "listing_state":"unavailable"},
                    ]})
                if path == "/api/search/pane-content":
                    batch = r.request.post_data_json
                    cache_only = bool(urlsplit(r.request.url).query)
                    (cache_checks if cache_only else batches).append([q["pane"] for q in batch])
                    if fail_one_batch and not cache_only:
                        fail_one_batch = False
                        return r.fulfill(status=503, json={})
                    response = client.post(path, params={"cached_only":cache_only}, json=batch)
                elif path == "/" or path.startswith("/assets/"):
                    response = client.get(path)
                elif path.startswith("/api/"):
                    return r.fulfill(json={"panes": [], "messages": [], "favorites": [], "items": [], "prefs": {}})
                else:
                    return r.fulfill(status=404, body="")
                r.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get("content-type", "application/octet-stream"))

            page.route("**/*", route)
            page.route_web_socket("**/*", lambda ws: ws.close())
            page.goto("http://tmuxweb.test/?pane=%25420", wait_until="domcontentloaded")
            expect(page.locator("#selected-number")).to_contain_text("420")
            page.locator("#menu").click()
            check_host_rows(page)
            expect(page.locator('.tree-host[data-tree="nas"] > summary .tree-count')).to_have_text("待获取")
            row = page.locator('.tree-host[data-tree="localhost"]')
            row.evaluate("e => e.classList.add('stale','offline')")
            assert "缓存" in row.locator(":scope > summary .tree-count").evaluate(
                "e => getComputedStyle(e,'::after').content")
            assert row.locator(".tree-session .tree-count").first.evaluate(
                "e => getComputedStyle(e,'::after').content") in ("none","normal")
            assert row.locator(":scope > summary").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            row.evaluate("e => e.classList.remove('stale','offline')")
            field = page.locator("#pane-search")
            count = page.locator("#pane-search-count")
            progress = page.locator("#pane-search-progress")
            page.evaluate("""() => TmuxPaneCache.saveTranscript('%442', {panePid:'1442', messages:[{text:'browsercached'}]})""")
            field.fill("#369 browsercached")
            expect(count).to_have_text("命中 1 个")
            expect(progress).to_contain_text("浏览器缓存命中 1")
            assert not cache_checks and not batches
            field.fill("unrelated")
            field.fill("mcphub")
            hit = page.locator('button[data-pane="%442"]')
            expect(count).to_have_text("命中 1 个", timeout=20000)
            expect(hit).to_be_visible()
            snippet = hit.locator(".pane-search-snippet")
            expect(snippet).to_contain_text("聊天 · MCPHub <img>")
            assert snippet.locator("img").count() == 0
            expect(progress).to_contain_text("正文 23/23")
            expect(progress).to_contain_text("本机 23")
            assert len(cache_checks)==1
            assert progress.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            assert len(batches) == 3 and "%442" in batches[-1], batches
            assert snippet.evaluate("el => el.clientWidth > 0 && el.scrollWidth <= el.clientWidth + 1")
            assert hit.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            field.fill("#369 mcphub")
            expect(count).to_have_text("命中 1 个")
            assert batches[-1] == ["%442"]
            if width == 390:
                fail_one_batch = True
                field.fill("MCPHUB")
                expect(count).to_have_text("命中 1 个 · 8 个会话未完整搜索")
                expect(hit).to_be_visible()
            if width == 390:
                # Browser misses and stale positives must still receive an authoritative read.
                page.evaluate("""() => {
                  const key='tmuxweb.cache.tx.%442',doc=JSON.parse(localStorage.getItem(key));
                  doc.at=Date.now()-60_000;localStorage.setItem(key,JSON.stringify(doc));
                }""")
                field.fill("#369 browsercached")
                expect(count).to_have_text("命中 0 个")
                expect(progress).to_contain_text("浏览器缓存命中 1")
                expect(hit).to_have_count(0)
                # Present a mixed fleet and hold the first online read open.
                page.evaluate("""() => {
                  const original=window.fetch;
                  window.testRoutes=[];
                  window.fetch=async(url,opts)=>{
                    if(!String(url).includes('/api/search/pane-content'))return original(url,opts);
                    const batch=JSON.parse(opts.body);
                    if(batch[0].terms[0]!=='pathfixture')return original(url,opts);
                    const cached=String(url).includes('cached_only');
                    const via=batch[0].pane==='%442'?'agent':'ssh';
                    if(!cached){
                      window.testRoutes.push(via);
                      if(via==='agent')await new Promise(resolve=>{window.releaseSearch=resolve;});
                    }
                    const results=batch.map(({pane})=>cached?{pane,next:pane==='%442'?'agent':'ssh'}:
                      {pane,source:'transcript',snippet:pane==='%442'?'pathfixture':null,routes:[via]});
                    return new Response(JSON.stringify({results}));
                  };
                }""")
                field.fill("pathfixture")
                expect(progress).to_contain_text("正在查询 节点连接器 · Local")
                page.wait_for_function("parseFloat(document.querySelector('#pane-search-progress').textContent.match(/([0-9.]+) 秒/)[1])>=1")
                assert page.evaluate("window.testRoutes")==["agent"]
                page.evaluate("window.releaseSearch()")
                expect(progress).to_contain_text("搜索完成")
                expect(progress).to_contain_text("SSH 回退 22")
                expect(progress).to_contain_text("节点连接器 1")
                assert page.evaluate("window.testRoutes")==["agent","ssh","ssh","ssh"]
                # Ignore a stale response even if the upstream finishes after cancellation.
                field.fill("pathfixture ")
                expect(progress).to_contain_text("正在查询 节点连接器")
                field.fill("#369 mcphub")
                expect(count).to_have_text("命中 1 个")
                page.evaluate("window.releaseSearch()")
                expect(snippet).to_contain_text("MCPHub")
            # Both a pending hover and an already visible preview must end on navigation.
            preview = page.locator("#hover-preview")
            if width == 1440:
                assert page.evaluate("TmuxPanePreview.delaySetting()") == 2000
                page.evaluate("localStorage.setItem('tmux-preview-delay', 'off')")
                hit.hover()
                page.wait_for_timeout(2200)
                expect(preview).to_be_hidden()
                page.mouse.move(0, 0)
                page.evaluate("localStorage.removeItem('tmux-preview-delay')")
            if width == 390:
                assert not page.evaluate("TmuxPanePreview.hoverable")
                hit.tap()
            else:
                hit.hover()
                if width == 1440:
                    expect(preview).to_be_visible()
                hit.click()
            expect(page.locator("#selected-number")).to_contain_text("369")
            page.wait_for_timeout(2200)
            expect(preview).to_be_hidden()
            page.locator("#menu").click()
            # Returning to the selected row must not re-open its preview.
            hit.hover()
            page.wait_for_timeout(2200)
            expect(preview).to_be_hidden()
            field.press("Escape")
            expect(field).to_have_value("")
            expect(count).to_be_hidden()
            expect(progress).to_be_hidden()
            if width == 1440:
                recent = page.locator('button[data-recent="%442"]')
                recent.hover()
                page.wait_for_timeout(2200)
                expect(preview).to_be_hidden()
            assert not fixture.tmux.sent and not fixture.tmux.keys
            page.close()
            print(f"{width}px pass", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
