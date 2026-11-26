"""Cross-host project navigation and relay receipts at every supported viewport."""
from pathlib import Path
import sys
import time
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.test_tmux_web import TmuxWebTests


def main():
    fixture=TmuxWebTests()
    fixture.setUp()
    try:
        with fixture.client as client, sync_playwright() as pw:
            fixture.login()
            browser=pw.chromium.launch()
            base=dict(fixture.tmux.panes[0], number=4, host_id='localhost')
            remote=dict(base,id='robo~%4',host_id='robo',number=125)
            for width in (1440,1180,1024,900,834,768,390):
                shared = '多主机测试'
                extra = '移动端回归-' + 'abcdefghijkl' * 3
                names = [shared]
                groups = {remote['id']: [shared]}
                page=browser.new_page(viewport={'width':width,'height':844},has_touch=width==390)
                def route(r):
                    u=urlsplit(r.request.url)
                    path=unquote(u.path)
                    if path=='/api/hosts':
                        data={'hosts':[{'id':'localhost','name':'Ubuntu','panes':[base]},
                                       {'id':'robo','name':'robo','panes':[remote]}]}
                    elif path=='/api/panes/status':
                        data={'observed_at':time.time(),'panes':[{'id':remote['id'],'status':'idle','needs_input':True,
                              'prompt':{'source':'script_status','kind':'notice','title':'脚本请求人工处理','read_only':True,
                                        'options':[],'revision':'test'}}],
                              'outbound':[{'child':remote['id'],'status':'running','progress':'blocked','question':'检查超时原因','created':1}]}
                    elif path=='/api/pane-projects':
                        if r.request.method == 'POST':
                            names.append(r.request.post_data_json['project'])
                            data={'created': True}
                        else:
                            data={'names':names, 'memberships':groups}
                    elif path.endswith('/projects'):
                        pane = path.split('/')[3]
                        changes = r.request.post_data_json
                        groups[pane] = sorted((set(groups.get(pane, [])) | set(changes['add'])) - set(changes['remove']))
                        data={'ok':True}
                    elif path.endswith('/transcript'):
                        data={'provider':'codex','messages':[{'id':'q','role':'user','text':'测试'}]}
                    elif path in ('/', '/home') or path.startswith('/assets/'):
                        response=client.get(path)
                        return r.fulfill(status=response.status_code,body=response.content,content_type=response.headers['content-type'])
                    else:
                        data={'panes':[],'items':[],'prefs':{},'messages':[]}
                    return r.fulfill(json=data)
                page.route('**/*',route)
                page.route_web_socket('**/*',lambda ws:ws.close())
                page.goto('http://tmuxweb.test/home',wait_until='domcontentloaded')
                if page.locator('#side-open').is_visible():
                    page.locator('#side-open').click()
                create=page.get_by_role('link',name='新建项目')
                assert create.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
                create.click()
                page.get_by_label('项目名称').fill(extra)
                page.get_by_role('button',name='创建',exact=True).click()
                empty_group=page.locator(f'details[data-tree="project:{extra}"]')
                expect(empty_group.locator('summary')).to_contain_text('· 0')
                page.goto('http://tmuxweb.test/?pane=%254',wait_until='domcontentloaded')
                expect(page.locator('#relay-receipts')).to_contain_text('等待人工处理')
                expect(page.locator('#relay-receipts a')).to_have_attribute('href','/?pane=robo~%254')
                page.locator('#menu').click()
                expect(page.get_by_role('button',name='新建项目')).to_be_visible()
                page.get_by_role('button',name='项目',exact=True).click()
                page.get_by_role('button',name='管理当前会话的项目').click()
                form=page.locator('#project-dialog')
                for name in (shared, extra):
                    box=form.get_by_label(name,exact=True)
                    box.check()
                    assert box.locator('..').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                assert base['id'] not in groups
                form.get_by_role('button',name='保存').click()
                expect(empty_group.locator('summary')).to_contain_text('· 1')
                page.get_by_role('button',name='管理当前会话的项目').click()
                expect(form.get_by_label(shared,exact=True)).to_be_checked()
                form.get_by_label(extra,exact=True).uncheck()
                form.get_by_role('button',name='保存').click()
                expect(empty_group.locator('summary')).to_contain_text('· 0')
                group=page.locator('details[data-tree="project:多主机测试"]')
                expect(group.locator('summary')).to_contain_text('· 2')
                group.locator('summary > span').click()
                expect(group.locator('.pane')).to_have_count(2)
                sizes=page.locator('.project-tools button,.project-jump').evaluate_all(
                    'buttons=>buttons.map(b=>[b.textContent,b.scrollWidth,b.clientWidth,getComputedStyle(b).display])')
                assert all(sw<=cw+1 for _,sw,cw,_ in sizes), (width,sizes)
                group.get_by_role('button',name='1 等你').click()
                expect(page.locator('#selected-number')).to_contain_text('125')
                page.close()
                print(f'{width}px pass',flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__=='__main__':
    main()
