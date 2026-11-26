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
                groups={remote['id']:'多主机测试'}
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
                        data={'projects':groups}
                    elif path.endswith('/project'):
                        groups[path.split('/')[3]]=r.request.post_data_json['project']
                        data={'ok':True}
                    elif path.endswith('/transcript'):
                        data={'provider':'codex','messages':[{'id':'q','role':'user','text':'测试'}]}
                    elif path=='/' or path.startswith('/assets/'):
                        response=client.get(path)
                        return r.fulfill(status=response.status_code,body=response.content,content_type=response.headers['content-type'])
                    else:
                        data={'panes':[],'items':[],'prefs':{},'messages':[]}
                    return r.fulfill(json=data)
                page.route('**/*',route)
                page.route_web_socket('**/*',lambda ws:ws.close())
                page.goto('http://tmuxweb.test/?pane=%254',wait_until='domcontentloaded')
                expect(page.locator('#relay-receipts')).to_contain_text('等待人工处理')
                expect(page.locator('#relay-receipts a')).to_have_attribute('href','/?pane=robo~%254')
                page.locator('#menu').click()
                page.locator('#project-view').click()
                field=page.locator('#pane-project-name')
                field.fill('多主机测试')
                page.locator('#pane-project-form button').click()
                group=page.locator('details[data-tree="project:多主机测试"]')
                expect(group.locator('summary')).to_contain_text('· 2')
                group.locator('summary > span').click()
                expect(group.locator('.pane')).to_have_count(2)
                sizes=page.locator('#project-view,#pane-project-form button,.project-jump').evaluate_all(
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
