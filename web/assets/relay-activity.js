/* A visible drawer reads task rows and an indexed result cursor, never pane transcripts. */
(() => {
  const $ = id => document.getElementById(id);
  function mount({api, pane, targets, label, close}) {
    const drawer = $('inbox-drawer'), entry = $('delegate-task'), report = $('task-report');
    const tasks = $('inbox-tasks'), results = $('inbox-results'), note = $('task-status');
    const feed = new Map();
    let view = 'pending', cursor = null, epoch = 0, timer = 0, loading = false;
    let lastTasks = '', lastResults = '', reporting = null, creating = false;
    const stages = {assigned:'已委派', working:'执行中', delivered:'已交付', failed:'执行失败',review:'待审阅',acknowledged:'已接手，待人工处理',declined:'已拒绝'};
    function stage(c) {
      if (c.status === 'error' || c.status === 'timeout') return '传递失败';
      if (c.task_stage === 'assigned') {
        if (c.received_at) return '已确认接收';
        if (c.progress === 'submitted') return '已提交 · 待确认接收';
      }
      return stages[c.task_stage] || (c.status === 'ok' ? '已回复' : '失败');
    }
    function button(text, action) {
      const el=document.createElement('button'); el.type='button'; el.textContent=text;
      el.onclick=action; return el;
    }
    function row(call, isResult) {
      const item=document.createElement('article'); item.className='task-row';
      const head=document.createElement('strong'), body=document.createElement('p'), link=document.createElement('a');
      head.textContent=label(call.parent)+' → '+label(call.child)+' · '+stage(call);
      body.textContent=call.task_result || call.error || call.reply_preview || call.question;
      const fileReview=['file_read','file_write'].includes(call.source);
      if(fileReview){
        head.textContent=(call.source==='file_read'?'文件读取申请':'文件写入申请')+' · '+stage(call);
        body.textContent='申请方填写：'+call.parent+' → '+label(call.child)+'\n'+call.question;
      }
      const received=!!call.received_at, ref=received || fileReview || call.source!=='message' ? call.child : call.parent;
      const url=new URL('/',location.href); url.searchParams.set('pane',ref);
      if(call.source==='message'){
        url.searchParams.set('relay',call.id);url.searchParams.set('side',received?'received':'sent');
        link.dataset.relayChat='';
      }
      link.href=url.pathname+url.search; link.textContent='打开对应 Chat'; link.onclick=close;
      item.append(head,body,link);
      if(fileReview && !isResult){
        const decide=async choice=>{
          try{
            await api('/api/relay/file-reviews/'+encodeURIComponent(call.id)+'/'+choice,{
              method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
            await refresh();
            window.TmuxNeedsYouInbox?.refreshCount();
          }catch(err){note.textContent=err.message;}
        };
        item.append(button('接手人工处理',()=>decide('acknowledged')),button('拒绝申请',()=>decide('declined')));
      } else if (isResult && !fileReview) {
        item.append(button('查看结果',async()=>{
          try {const d=await api('/api/agents/calls/'+encodeURIComponent(call.id)); body.textContent=d.task_result || d.reply || d.error || call.question;}
          catch(err){note.textContent=err.message;}
        }));
      } else if(call.child===pane() && call.status==='ok' && call.progress==='submitted'){
        if(call.task_stage==='assigned') item.append(button('开始执行',e=>update(call,'working','',e.currentTarget)));
        if(call.task_stage==='working') item.append(button('报告结果',()=>{
          reporting=call; report.hidden=false; $('task-result-text').value=''; $('task-result-text').focus();
        }));
      }
      return item;
    }
    async function update(call,stage,result,control) {
      if (call.child !== pane()) { note.textContent='请在接收 Chat 更新此任务'; return; }
      control.disabled=true;
      try {
        await api('/api/relay/tasks/'+encodeURIComponent(call.id),{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({pane:pane(),stage,result})});
        report.hidden=true; reporting=null; note.textContent='已记录'; await refresh();
      } catch(err){note.textContent=err.message;}
      finally{control.disabled=false;}
    }
    async function refresh() {
      if(loading || drawer.hidden || document.hidden)return;
      const serial=epoch, id=pane(); loading=true;
      try {
        if(view==='pending'){
          const data=await api('/api/relay/tasks');
          if(serial!==epoch || id!==pane())return;
          const rows=(data.items||[]).filter(c=>['assigned','working','review'].includes(c.task_stage) && !['error','timeout'].includes(c.status));
          const key=JSON.stringify([id,rows]);
          if(key!==lastTasks){tasks.replaceChildren(...rows.map(c=>row(c,false)));lastTasks=key;}
        }else{
          const data=await api('/api/relay/results'+(cursor===null?'':'?after='+cursor));
          if(serial!==epoch)return;
          cursor=data.cursor ?? cursor;
          for(const c of data.items||[]) feed.set(c.id,c);
          while(feed.size>100) feed.delete(feed.keys().next().value);
          const rows=[...feed.values()].reverse(), key=JSON.stringify(rows);
          if(key!==lastResults){results.replaceChildren(...rows.map(c=>row(c,true)));lastResults=key;}
          $('relay-no-results').hidden=!!rows.length || !!document.querySelector('#inbox-activity .inbox-item');
        }
      }catch(err){if(serial===epoch)note.textContent=err.message;}
      finally{if(serial===epoch)loading=false;}
    }
    function tab(which){
      view=which; epoch++;loading=false;
      $('inbox-pending-section').hidden=which!=='pending';
      $('inbox-result-section').hidden=which!=='results';
      for(const btn of drawer.querySelectorAll('[data-inbox-tab]'))btn.setAttribute('aria-pressed',String(btn.dataset.inboxTab===which));
      note.textContent=''; refresh();
    }
    for(const btn of drawer.querySelectorAll('[data-inbox-tab]'))btn.onclick=()=>tab(btn.dataset.inboxTab);
    $('new-task').onclick=()=>{
      entry.hidden=!entry.hidden;
      if(!entry.hidden){
        const choices=targets().filter(p=>p.id!==pane());
        $('task-target').replaceChildren(...choices.map(p=>new Option(label(p.id),p.id)));
        $('task-text').focus();
      }
    };
    entry.onsubmit=async e=>{
      e.preventDefault(); if(creating || !pane() || !$('task-target').value)return;
      const id=pane(),target=$('task-target').value,text=$('task-text').value.trim();
      if(!text)return;
      creating=true; $('task-create').disabled=true;
      try {
        const reply=await api('/api/relay/message',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({caller:id,target,text,submit:true,delegate:true})});
        entry.hidden=true; $('task-text').value='';
        note.textContent=reply.submitted?'任务已提交':'任务已记录，等待提交确认'; await refresh();
      }catch(err){note.textContent=err.message+'；重试前请核对任务记录';}
      finally{creating=false;$('task-create').disabled=false;}
    };
    report.onsubmit=e=>{
      e.preventDefault();const output=$('task-result-text').value.trim();
      if(reporting && output && e.submitter)update(reporting,e.submitter.value,output,e.submitter);
    };
    return {
      open(){clearInterval(timer);tab(view);timer=setInterval(refresh,9000);},
      close(){epoch++;loading=false;clearInterval(timer);entry.hidden=true;report.hidden=true;reporting=null;},
      refresh
    };
  }
  window.TmuxRelayActivity={mount};
})();
