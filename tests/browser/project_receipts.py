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
            remote=dict(base,id='robo~%4',host_id='robo',number=125,session='build',window_name='review',pane_id='%4')
            workers=[dict(base,id=f'%{n}',number=n,title=f'分页测试会话 {n}') for n in range(300,345)]
            for width in (1440,1180,1024,900,834,768,390):
                shared = '多主机测试'
                extra = '移动端回归-' + 'abcdefghijkl' * 3
                failed_removals=[]
                changes_sent=[]
                names = [shared]
                groups = {remote['id']: [shared]}
                page=browser.new_page(viewport={'width':width,'height':844},has_touch=width==390)
                def route(r):
                    u=urlsplit(r.request.url)
                    path=unquote(u.path)
                    if path=='/api/hosts':
                        data={'hosts':[{'id':'localhost','name':'Ubuntu','panes':[base,*workers]},
                                       {'id':'robo','name':'robo','panes':[remote]}]}
                    elif path=='/api/home/recent':
                        data={'recent':[{'pane':remote['id'],'number':125,'label':'夜间回归','pinned':True}]}
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
                        changes_sent.append((pane,changes))
                        if failed_removals and changes.get('remove')==[shared]:
                            failed_removals.pop()
                            return r.fulfill(status=503,json={'detail':'测试移出失败'})
                        groups[pane] = sorted((set(groups.get(pane, [])) | set(changes.get('add', []))) - set(changes.get('remove', [])))
                        data={'pane':pane,'projects':groups[pane]}
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
                create=page.get_by_role('link',name='项目',exact=True)
                assert create.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
                create.click()
                page.get_by_role('button',name='新建项目').click()
                page.get_by_label('项目名称').fill(extra)
                page.get_by_role('button',name='创建',exact=True).click()
                empty_group=page.locator(f'details[data-tree="project:{extra}"]')
                expect(empty_group.locator('summary')).to_contain_text('· 0')
                page.goto('http://tmuxweb.test/?pane=%254',wait_until='domcontentloaded')
                expect(page.locator('#relay-receipts')).to_contain_text('等待人工处理')
                expect(page.locator('#relay-receipts a')).to_have_attribute('href','/?pane=robo~%254')
                page.locator('#menu').click()
                page.get_by_role('button',name='主机',exact=True).click()
                expect(page.get_by_role('button',name='新建项目')).to_be_hidden()
                page.get_by_role('button',name='项目',exact=True).click()
                group=page.locator('details[data-tree="project:多主机测试"]')
                editor=page.locator('#project-editor')
                def click(button):
                    button.tap() if width==390 else button.click()
                click(empty_group.get_by_role('button', name=f'管理 {extra} 的成员'))
                expect(editor).to_be_visible()
                assert not page.locator('dialog[open]').count()
                search=editor.get_by_role('searchbox')
                result=editor.locator('.project-candidate')
                search.fill('分页测试')
                expect(result).to_have_count(40)
                more=editor.get_by_role('button',name='显示更多')
                click(more)
                expect(result).to_have_count(45)
                expect(result.nth(40).get_by_role('button')).to_be_focused()
                assert editor.locator('#project-results').evaluate('el=>el.scrollTop>0 && el.scrollWidth<=el.clientWidth+1')
                search.fill('robo build review %4 #125 夜间')
                expect(result).to_have_count(1)
                expect(result).to_contain_text('夜间回归')
                click(result.get_by_role('button',name='加入'))
                expect(result.get_by_role('button',name='移出')).to_be_enabled()
                assert set(groups[remote['id']])=={shared,extra}
                search.fill('robo #12')
                expect(result).to_have_count(0)
                search.fill('夜间')
                click(result.get_by_role('button',name='移出'))
                expect(result.get_by_role('button',name='加入')).to_be_enabled()
                click(editor.get_by_role('button',name='返回分组'))
                expect(empty_group.locator('summary')).to_contain_text('· 0')
                click(group.locator('summary > span'))
                remove=group.get_by_role('button',name=f'从 {shared} 移出 #125')
                expect(remove.locator('use')).to_have_attribute('href','#i-trash')
                failed_removals.append(True)
                click(remove)
                expect(page.locator('#toast')).to_contain_text('测试移出失败')
                expect(remove).to_be_enabled()
                assert groups[remote['id']]==[shared]
                click(remove)
                expect(group.locator('summary')).to_contain_text('· 0')
                assert changes_sent[-1]==(remote['id'],{'remove':[shared]})
                expect(editor).to_be_hidden()
                ungrouped=page.locator('details[data-tree="project:"]')
                click(ungrouped.locator('summary > span'))
                expect(ungrouped.locator('[data-remove-member]')).to_have_count(0)
                assign=ungrouped.get_by_role('button',name='为 #125 选择项目')
                click(assign)
                editor.get_by_label(shared,exact=True).check()
                click(editor.get_by_role('button',name='取消'))
                assert not groups[remote['id']]
                click(assign)
                editor.get_by_label(shared,exact=True).check()
                editor.get_by_label(extra,exact=True).check()
                names.append('另一用户的组')
                groups[remote['id']].append(names[-1])
                editor.get_by_role('button',name='保存').click()
                expect(group.locator('summary')).to_contain_text('· 1')
                remove.press('Enter') if width!=390 else remove.tap()
                expect(group.locator('summary')).to_contain_text('· 0')
                expect(empty_group.locator('summary')).to_contain_text('· 1')
                assert set(groups[remote['id']])=={extra,'另一用户的组'}
                expect(page.locator('#selected-number')).to_have_text('#4')
                click(empty_group.get_by_role('button',name=f'管理 {extra} 的成员'))
                search.fill('Ubuntu #4')
                click(result.get_by_role('button',name='加入'))
                expect(result.get_by_role('button',name='移出')).to_be_enabled()
                click(editor.get_by_role('button',name='返回分组'))
                expect(empty_group.locator('summary')).to_contain_text('· 2')
                expect(page.locator('#selected-number')).to_contain_text('4')
                click(empty_group.locator('summary > span'))
                expect(empty_group.locator('.pane')).to_have_count(2)
                sizes=page.locator('.project-tools button,.project-jump,.project-manage').evaluate_all(
                    'buttons=>buttons.map(b=>[b.textContent,b.scrollWidth,b.clientWidth,getComputedStyle(b).display])')
                assert all(sw<=cw+1 for _,sw,cw,_ in sizes), (width,sizes)
                empty_group.get_by_role('button',name='1 等你').click()
                expect(page.locator('#selected-number')).to_contain_text('125')
                page.close()
                print(f'{width}px pass',flush=True)
            browser.close()
    finally:
        fixture.tearDown()


if __name__=='__main__':
    main()
