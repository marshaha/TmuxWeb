import {AnsiUp} from '/assets/ansi_up.js?v=1';
import {marked} from '/assets/marked.esm.js?v=1';
(() => {
  const ansi=new AnsiUp();
  const state={hosts:[],panes:[],treeOpen:new Set(),pendingMessages:new Map(),transcripts:new Map(),transcriptsEdit:new Map(),drafts:new Map(),transcriptMessages:[],transcriptLimit:300,hasMore:false,loadingEarlier:false,selected:null,ws:null,termWs:null,stick:true,mode:'chat',xterm:null,fit:null,resizeObserver:null,hiddenTimer:null,transcriptTimer:null,transcriptFailures:0,transcriptMtime:null,transcriptSig:null,filesPane:null,filesPicked:new Set(),filesAnchor:null,terminalMods:{ctrl:false,alt:false},kbShift:false,kbSymbols:false,viewportFitTimers:[],paneState:new Map(),stateAt:0,filesMode:false,filesPath:null,filesData:null,filesHidden:false,favorites:new Set(),livePaused:false,capturePending:null,captureTimer:null,capturePainted:0,captureFitWidth:0,captureFitSize:0,serverVersion:'',versionToast:{shownFor:'',dismissedFor:'',visible:false},versionPollRound:0};
  const $=id=>document.getElementById(id),list=$('pane-list'),term=$('terminal'),scroll=$('scroll'),input=$('input'),send=$('send');
  try{const saved=JSON.parse(localStorage.getItem('tmux-tree-open')||'[]');if(Array.isArray(saved))state.treeOpen=new Set(saved.filter(key=>typeof key==='string'));}catch{localStorage.removeItem('tmux-tree-open');}
  function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  // 附件：和 /preview（多窗口里的窗口）共用 /assets/composer-files.js 这一份实现
  let composerFiles=null;
  const tailAnsi=new AnsiUp();
  let tailLoop=null, collapsedTailPane=null, expandedForm='';
  function panePathOf(pane){
    const meta=(state.panes||[]).find(item=>item.id===pane);
    return (meta&&meta.path)||'';
  }
  function mountComposerFiles(){
    if(!window.TmuxFiles)return;
    composerFiles=window.TmuxFiles.mount({
      strip:$('uploads'),button:$('attach'),picker:$('upload-input'),drop:document.querySelector('.composer'),
      pane:()=>state.selected||'',path:()=>panePathOf(state.selected||''),
      onError:message=>showError(message),
      onBusy:busy=>{const send=$('send');if(send)send.disabled=!state.selected||busy;},
    });
  }
  function attachmentsPrompt(){return composerFiles?composerFiles.prompt():'';}
  function attachmentsPending(){return composerFiles?composerFiles.pending():false;}
  function clearAttachments(){if(composerFiles)composerFiles.clear();}
  function takeAttachments(){return composerFiles?composerFiles.take():[];}
  function restoreAttachments(list){if(composerFiles)composerFiles.restore(list);}
  function discardAttachments(list){if(composerFiles)composerFiles.discard(list);}
  function syncAttachments(){if(composerFiles)composerFiles.syncButton();}
  function showError(msg){if(msg)hideLoadCard();const el=$('error');el.textContent=msg;el.style.display=msg?'block':'none';}
  function isMobile(){return matchMedia('(max-width:760px)').matches;}
  // 触屏设备（含 iPad 这种宽度 > 760 的）一律不自动聚焦输入框：一进聊天就弹键盘很讨厌
  function isTouch(){return matchMedia('(pointer:coarse)').matches
      ||(matchMedia('(hover:none)').matches&&navigator.maxTouchPoints>0);}
  function canAutoFocusComposer(){return !isMobile()&&!isTouch();}
  function openSidebar(){if(isMobile()){$('sidebar').classList.add('open');$('drawer-backdrop').classList.add('show');}else{document.querySelector('.app').classList.remove('sidebar-collapsed');localStorage.setItem('tmux-sidebar-collapsed','0');}}
  function closeSidebar(){
    window.TmuxPanePreview?.hide();
    if(isMobile()){$('sidebar').classList.remove('open');$('drawer-backdrop').classList.remove('show');}else{document.querySelector('.app').classList.add('sidebar-collapsed');localStorage.setItem('tmux-sidebar-collapsed','1');}}
  // Terminal sizing. A soft keyboard firing visualViewport/resize/ResizeObserver for ~250ms used to refit and
  // repaint six times, each fit sending a new rows/cols to the PTY (tmux then redraws the whole screen).
  // Now: fit once now (so the box lifts with the keyboard), once when it settles, and only notify the PTY
  // when cols/rows actually changed.
  let termSizeTimer=null,termSizeSent='';
  function notifyTermSize(){
    clearTimeout(termSizeTimer);
    termSizeTimer=setTimeout(()=>{
      if(state.mode!=='terminal'||!state.xterm||state.termWs?.readyState!==WebSocket.OPEN)return;
      const xterm=state.xterm,signature=`${xterm.cols}x${xterm.rows}`;
      if(signature===termSizeSent)return;
      termSizeSent=signature;
      state.termWs.send(JSON.stringify({type:'resize',cols:xterm.cols,rows:xterm.rows}));
      // the repaint lands at the final size: show its last rows
      setTimeout(()=>{try{xterm.scrollToBottom();}catch{};try{xterm.refresh(0,Math.max(0,xterm.rows-1));}catch{}},120);
    },250);
  }
  function fitTerminalNow(){
    if(state.mode!=='terminal'||!state.fit||!state.xterm||document.hidden)return;
    const xterm=state.xterm,cols=xterm.cols,rows=xterm.rows;
    const viewport=xterm.element?.querySelector('.xterm-viewport');
    const atBottom=!viewport||viewport.scrollTop+viewport.clientHeight>=viewport.scrollHeight-8,scrollTop=viewport?viewport.scrollTop:0;
    try{state.fit.fit();}catch{return;}
    // Repaint every row on each settle fit. The grid often keeps its cols/rows while the
    // canvas was resized under it (keyboard animations move the box by fractions of a cell),
    // and then only the rows tmux happened to redraw came back -- on a phone that looked like
    // "only the last line survived". This runs a handful of times per transition, not per frame.
    try{xterm.refresh(0,Math.max(0,xterm.rows-1));}catch{}
    if(xterm.cols===cols&&xterm.rows===rows){notifyTermSize();return;}
    if(!atBottom&&viewport)viewport.scrollTop=scrollTop;else if(viewport)viewport.scrollTop=viewport.scrollHeight;
    // Tell the PTY once, after the size settles. Sending every intermediate size made tmux paint
    // at e.g. 28 rows while the box was still animating, so the rows of the final repaint were
    // written into an xterm that had already scrolled them away -- the phone then showed a screen
    // that started eleven lines late with a black hole below.
    notifyTermSize();
  }
  function scheduleTerminalFit(){if(state.mode!=='terminal'||!state.fit||!state.xterm)return;state.viewportFitTimers.forEach(clearTimeout);state.viewportFitTimers=[0,300].map(delay=>setTimeout(()=>requestAnimationFrame(fitTerminalNow),delay));}
  function syncVisualViewport(){const viewport=window.visualViewport,height=viewport?viewport.height:window.innerHeight;document.documentElement.style.setProperty('--visual-height',`${Math.round(height)}px`);document.documentElement.style.setProperty('--visual-top',`${Math.round(viewport?.offsetTop||0)}px`);}
  function handleVisualViewportChange(){syncVisualViewport();scheduleTerminalFit();}
  // The composer and the terminal's input row both want the keyboard-open layout;
  // decide from the focused element so one field's blur cannot cancel the other.
  // The keyboard's final height can arrive well after blur (iOS reports it late), so re-apply the
  // layout a few times; the fit itself is a no-op unless cols/rows really changed.
  function settleViewport(){syncVisualViewport();fitCapture();scheduleTerminalFit();
    [140,400,800].forEach(delay=>setTimeout(()=>{syncVisualViewport();fitCapture();scheduleTerminalFit();},delay));}
  function keyboardWanted(){return document.activeElement===input || document.activeElement===$('terminal-input');}
  function setComposing(active){if(!isMobile())return;document.querySelector('.app').classList.toggle('mobile-composing',active);settleViewport();if(active)setTimeout(()=>{input.scrollIntoView({block:'nearest'});input.setSelectionRange(input.value.length,input.value.length);},60);}
  function toast(message,ms=2600){const el=$('toast');if(!el)return;state.versionToast.visible=false;el.classList.remove('has-action');el.textContent=message;el.hidden=false;
    requestAnimationFrame(()=>el.classList.add('show'));clearTimeout(toast.timer);
    toast.timer=setTimeout(()=>{el.classList.remove('show');toast.timer=setTimeout(()=>{el.hidden=true;},200);},ms);}
  // ---- 版本轮询（借 Open WebUI：svelte.config.js pollInterval:60000 + src/routes/+layout.svelte:111——
  // 服务端发版后，旧客户端自己发现并提示刷新。TmuxWeb 不学它静默强制刷新：用户可能正在输入或看输出，
  // 整页刷新会丢草稿与滚动位置，所以只弹 toast，给刷新按钮；点“稍后”后同一版本不再打扰。
  // 节奏复用现有的 20s 一轮 loadPanes：每 3 轮查一次版本 ≈ 60s，与 Open WebUI 的 60s 轮询同频，
  // 不另起定时器；切到后台标签时 loadPanes 本来就跳过，版本检查也跟着停。
  const VERSION_CHECK_EVERY_ROUNDS=3;
  function serverVersionBaseline(){
    const meta=document.querySelector('meta[name="server-version"]');
    const value=meta?String(meta.content||'').trim():'';
    return value&&!value.startsWith('__')?value:'';
  }
  function shouldCheckVersion(round,every){return round>0&&round%every===0;}
  function shouldShowVersionToast(baseline,fetched,shownFor,dismissedFor,visible){
    if(!baseline||!fetched||baseline===fetched)return false;
    if(dismissedFor===fetched)return false;
    if(shownFor===fetched&&visible)return false;
    return true;
  }
  function showVersionToast(version){
    const el=$('toast');if(!el)return;
    clearTimeout(toast.timer); // 别让一条普通 toast 的隐藏定时器半路收走这个常驻提示
    state.versionToast.shownFor=version;state.versionToast.visible=true;
    el.classList.add('has-action');
    el.textContent='';
    const label=document.createElement('span');
    label.textContent='有新版本';
    const refresh=document.createElement('button');
    refresh.type='button';refresh.textContent='刷新';
    refresh.onclick=()=>{location.reload();};
    const later=document.createElement('button');
    later.type='button';later.textContent='稍后';later.classList.add('ghost');
    later.onclick=()=>{state.versionToast.dismissedFor=version;state.versionToast.visible=false;el.classList.remove('show');toast.timer=setTimeout(()=>{el.hidden=true;},200);};
    el.append(label,refresh,later);
    el.hidden=false;
    requestAnimationFrame(()=>el.classList.add('show'));
  }
  async function checkServerVersion(){
    // 不走 api()：它在 401 时会直接跳登录页、非 200 时抛错；版本检查必须在旧服务端
    // （404，无此端点）、会话过期（401）、网络失败时都静默跳过，下轮再查。
    try{
      const response=await fetch('/api/version',{headers:{'Cache-Control':'no-cache'}});
      if(!response.ok)return;
      const data=await response.json();
      const fetched=typeof data?.version==='string'?data.version.trim():'';
      if(!fetched)return;
      if(!state.serverVersion){state.serverVersion=fetched;return;} // 页面没带基线：认领当前版，不提示
      if(!shouldShowVersionToast(state.serverVersion,fetched,state.versionToast.shownFor,state.versionToast.dismissedFor,state.versionToast.visible))return;
      showVersionToast(fetched);
    }catch{/* 网络失败或响应不是 JSON：静默跳过，下轮再查 */}
  }
  // ---- 网络状态灯：绿=通畅，黄=吃力，红=不通
  const netWatch={samples:[],offline:false};
  function netSample(ms,ok){
    netWatch.samples.push({ms:Math.round(ms),ok});
    if(netWatch.samples.length>8)netWatch.samples.shift();
    paintNetDot();
  }
  function netStateOf(){
    if(typeof navigator!=='undefined'&&navigator.onLine===false)return 'down';
    const recent=netWatch.samples.slice(-6);
    if(!recent.length)return 'ok';
    if(recent.some(item=>!item.ok))return 'down';
    // 红只留给"真的不通"（请求失败 / 离线）；慢就是黄
    const average=recent.reduce((sum,item)=>sum+item.ms,0)/recent.length;
    return average>800?'busy':'ok';
  }
  function paintNetDot(){
    const dot=$('net-dot');if(!dot)return;
    const state=netStateOf();
    const recent=netWatch.samples.slice(-6);
    const average=recent.length?Math.round(recent.reduce((sum,item)=>sum+item.ms,0)/recent.length):0;
    const label=state==='ok'?'网络：通畅':state==='busy'?'网络：吃力':'网络：不通';
    dot.className='net-dot'+(state==='ok'?'':' '+state);
    const text=average?`${label}（最近 ${average}ms）`:label;
    dot.title=text;dot.setAttribute('aria-label',text);
  }
  // 给测试用（函数都在闭包里）
  window.TmuxNet={sample:netSample,state:netStateOf,paint:paintNetDot};
  window.addEventListener('offline',()=>{netWatch.offline=true;paintNetDot();});
  window.addEventListener('online',()=>{netWatch.offline=false;netWatch.samples=[];paintNetDot();});

  // ---- 远端加载卡：pane 在子节点/慢链路上时，别让人对着空白等
  // 远端加载的状态就显示在 idle / thinking 那一行：加载时状态本身没有意义，
  // 再弹一张卡片反而把界面切开。这里只维护数据，画由 syncProgress() 统一负责。
  const loadCard={shownAt:0,bytes:0,total:0,started:0,kind:'',label:'',tick:null,timer:null};
  function formatBytes(n){if(!n)return '0 B';if(n<1024)return `${n} B`;if(n<1024*1024)return `${(n/1024).toFixed(1)} KB`;return `${(n/1024/1024).toFixed(1)} MB`;}
  function paneLoadLabel(id){const found=state.panes.find(p=>p.id===id);if(!found)return id;const host=state.hosts.find(h=>h.id===(found.host_id||''))||{};const name=host.prefix?`${host.prefix}#${found.number}`:(found.number!=null?`#${found.number}`:id);return `${name} · ${host.name||host.id||''}`.trim();}
  function loadingActive(){return !!loadCard.shownAt;}
  function loadingLabel(){return loadCard.kind==='fed'?'子节点加载中':loadCard.kind==='remote'?'远端加载中':'加载中';}
  function loadingDetail(){const elapsed=(performance.now()-loadCard.started)/1000;
    const percent=loadCard.total>0?Math.min(100,Math.round(loadCard.bytes/loadCard.total*100)):null;
    const parts=[`${elapsed.toFixed(1)}s`];
    if(percent!=null)parts.push(`${percent}%`);
    if(loadCard.bytes)parts.push(formatBytes(loadCard.bytes)+(loadCard.total>0?` / ${formatBytes(loadCard.total)}`:''));
    const speed=elapsed>0.35?loadCard.bytes/elapsed:0;
    if(speed)parts.push(`${formatBytes(Math.round(speed))}/s`);
    return parts.join(' · ');}
  function showLoadCard(id){
    loadCard.shownAt=Date.now();loadCard.started=performance.now();loadCard.bytes=0;loadCard.total=0;
    loadCard.label=paneLoadLabel(id);
    const found=state.panes.find(p=>p.id===id)||{};
    const host=state.hosts.find(h=>h.id===(found.host_id||''))||{};
    loadCard.kind=host.kind||'';
    syncProgress();
    clearInterval(loadCard.tick);loadCard.tick=setInterval(syncProgress,200);
    clearTimeout(loadCard.timer);loadCard.timer=setTimeout(()=>{if(loadCard.shownAt&&Date.now()-loadCard.shownAt>45000)hideLoadCard();},48000);
  }
  function updateLoadCard(info){loadCard.bytes=info.received||0;loadCard.total=info.total||0;syncProgress();}
  // 给测试用的钩子（页面里的函数都在闭包里，外面看不到）
  window.TmuxLoadCard={show:showLoadCard,hide:hideLoadCard,update:updateLoadCard};
  // 顶栏按钮排真溢出时才加 .scrolls（画左侧分界阴影）：否则"能滑"和"就是这排"看不出区别
  function syncTopActionsScroll(){
    const acts=document.querySelector('.top-actions');if(!acts)return;
    acts.classList.toggle('scrolls',acts.scrollWidth>acts.clientWidth+1);
  }
  (function watchTopActions(){
    const acts=document.querySelector('.top-actions');if(!acts)return;
    acts.addEventListener('scroll',syncTopActionsScroll,{passive:true});
    window.addEventListener('resize',syncTopActionsScroll);
    syncTopActionsScroll();
    if(window.ResizeObserver)new ResizeObserver(syncTopActionsScroll).observe(acts);
    setTimeout(syncTopActionsScroll,600);
  })();
  // "本轮"的边界 = 最后一条用户消息。授权用 turn 作用域时靠它判断过期。
  window.TmuxTurn={key:()=>{const list=state.transcriptMessages||[];for(let i=list.length-1;i>=0;i--){const m=list[i];if(m&&(m.role==='user'||m.kind==='user'))return String(m.id||m.uuid||'');}return '';}};
  function hideLoadCard(){if(!loadCard.shownAt)return;loadCard.shownAt=0;loadCard.kind='';loadCard.label='';
    clearInterval(loadCard.tick);clearTimeout(loadCard.timer);syncProgress();}

  // 带进度的 JSON 请求：读流、算百分比和速度。拿不到 body 就退回普通 api()。
  async function apiProgress(url,onProgress){
    const started=performance.now();
    const response=await fetch(url,{credentials:'same-origin'});
    if(response.status===401){location='/login';throw new Error('登录已失效');}
    if(!response.ok){let detail=`HTTP ${response.status}`;try{detail=(await response.json()).detail||detail;}catch{}throw new Error(detail);}
    const total=Number(response.headers.get('content-length')||0);
    if(!response.body||typeof response.body.getReader!=='function'){
      const text=await response.text();onProgress&&onProgress({received:text.length,total:total||text.length,elapsed:(performance.now()-started)/1000});
      return JSON.parse(text);
    }
    const reader=response.body.getReader();const chunks=[];let received=0;
    for(;;){const {done,value}=await reader.read();if(done)break;
      chunks.push(value);received+=value.length;
      onProgress&&onProgress({received,total,elapsed:(performance.now()-started)/1000});}
    const text=await new Blob(chunks).text();
    return JSON.parse(text);
  }

  async function api(url,opts={}){
    const started=performance.now();
    let response;
    try{response=await fetch(url,opts);}catch(error){if(!opts.signal?.aborted)netSample(performance.now()-started,false);throw error;}
    netSample(performance.now()-started,response.ok||response.status===304);
    const r=response;if(r.status===401){location='/login';throw Object.assign(new Error('登录已失效'),{status:401});}if(!r.ok){let d={};try{d=await r.json()}catch{}throw Object.assign(new Error(d.detail||`HTTP ${r.status}`),{status:r.status});}return r.json();}
  async function refreshHost(hostId,button){
    if(!hostId)return;
    if(button)button.classList.add('busy');
    try{
      const d=await api(`/api/hosts?refresh=1&host=${encodeURIComponent(hostId)}`,{cache:'no-store'});
      applyHosts(d);
      const host=(d.hosts||[]).find(item=>item.id===hostId);
      if(host&&host.error)showError(`${host.name||hostId}: ${host.error}`);else showError('');
    }catch(e){showError(e.message);}
    finally{if(button)button.classList.remove('busy');}
  }
  // 冷启动先把「上次看到的服务器树」画出来：从 /hosts 返回时不再白屏等一轮 SSH 探测
  function paintCachedHosts(){
    if(!window.TmuxPaneCache)return false;
    const cached=TmuxPaneCache.readHosts(6*3600*1000);
    if(!cached)return false;
    state.hosts=cached.hosts;
    state.panes=state.hosts.flatMap(host=>(host.panes||[]).map(p=>({...p,host_id:p.host_id||host.id})));
    if(window.TmuxRecents)TmuxRecents.setHosts(state.hosts);
    $('pane-count').textContent=`${state.hosts.length} hosts · ${state.panes.length} panes`;
    renderList();
    state.staleSnapshot=true;
    // 顺手把上次看的 pane 也选上：聊天气泡同样先从本地缓存画出来，
    // 不用等 /api/hosts（SSH 探测慢的时候那几秒就是"全量刷新"的观感）
    applyWantedPane();
    return true;
  }

  // 副标题只拼"真的有值"的字段。以前直接拼 p.target/p.command，缓存里少字段（或
  // 联邦/新建 pane 来不及带全）就会在顶栏印出 "undefined · undefined · /path"。
  function paneHeaderParts(p){
    const parts=[];
    const push=value=>{const text=(value==null?'':String(value)).trim();if(text&&!parts.includes(text))parts.push(text);};
    push(p&&p.target);
    push(p&&p.command);
    push(p&&p.path);
    if(!parts.length)push(p&&p.session);
    return parts;
  }
  function paintPaneHeader(p){
    const sub=$('selected-sub');if(!sub)return;
    sub.textContent=paneHeaderParts(p).join(' · ')||'（缺少窗格信息）';
  }
  function applyHosts(d){state.hosts=d.hosts||[];if(window.TmuxRecents)TmuxRecents.setHosts(state.hosts);state.panes=state.hosts.flatMap(host=>(host.panes||[]).map(p=>({...p,host_id:p.host_id||host.id})));$('pane-count').textContent=`${state.hosts.length} hosts · ${state.panes.length} panes`;renderHostList();applyWantedPane();repaintSelectedHeader();loadFavorites();refreshPaneStates();}

  /** hosts 刷新后把已选中 pane 的顶栏对齐真数据（缓存先画过头时不会一直停在旧值）。 */
  function repaintSelectedHeader(){
    if(!state.selected){return;}
    const live=state.panes.find(p=>p.id===state.selected);
    if(!live){return;}
    paintPaneHeader(live);
    $('share-button').disabled=!live.capabilities?.share;
    const num=$('selected-number');
    if(num&&live.number){num.hidden=false;num.textContent=paneLabel(live);}
  }
  async function loadPanes(force){try{const d=await api('/api/hosts'+(force?'?refresh=1':''));state.hosts=d.hosts||[];
      if(window.TmuxPaneCache&&d.hosts)TmuxPaneCache.saveHosts(d.hosts);if(window.TmuxRecents)TmuxRecents.setHosts(state.hosts);state.panes=state.hosts.flatMap(host=>(host.panes||[]).map(p=>({...p,host_id:p.host_id||host.id})));const valid=new Set();for(const host of state.hosts){valid.add(host.id);for(const p of host.panes||[]){valid.add(`${host.id}/${p.session}`);valid.add(`${host.id}/${p.session}/${p.window}:${p.window_name||''}`);}}state.treeOpen=new Set([...state.treeOpen].filter(key=>valid.has(key)));localStorage.setItem('tmux-tree-open',JSON.stringify([...state.treeOpen]));$('pane-count').textContent=`${state.hosts.length} hosts · ${state.panes.length} panes`;renderHostList();applyWantedPane();loadFavorites();refreshPaneStates();if(state.selected&&!state.panes.some(p=>p.id===state.selected)){
        // A partial hosts response can temporarily omit a remote pane.
        state.wantedPane=state.selected;
        selectPane(null, {remember:false});
      }
      if(state.wantedPane)$('selected-title').textContent='等待窗口 '+state.wantedPane;
    }catch(e){showError(e.message);}}

  function applyWantedPane(){
    const wanted=state.wantedPane;
    if(!wanted)return;
    let pane=state.panes.find(p=>p.id===wanted);
    if(!pane && /^#?\d{1,5}$/.test(wanted.trim())){
      const number=Number(wanted.replace('#',''));
      pane=state.panes.find(p=>p.number===number);
    }
    if(pane)selectPane(pane.id);
  }

  function replaceConsoleQuery(edit){
    const url=new URL(location.href);
    edit(url.searchParams);
    if(url.href!==location.href)history.replaceState(history.state,'',url);
  }

  function rememberPane(id){
    // The URL belongs to this tab. Shared storage is only a default for a new bare URL.
    replaceConsoleQuery(params=>{
      if(id){
        params.set('pane',id);
        params.set('mode',state.mode);
      }else{
        params.delete('pane');
        params.delete('mode');
      }
    });
    localStorage.setItem('tmux-last-pane',id||'');
  }
  function syncFavButton(){syncAttachments();const btn=$('fav-toggle');if(!btn)return;const gridButton=$('grid-add');if(gridButton)gridButton.disabled=!state.selected;const on=!!state.selected&&state.favorites.has(state.selected);btn.disabled=!state.selected;const closeButton=$('pane-close');if(closeButton)closeButton.disabled=!state.selected;btn.classList.toggle('on',on);btn.setAttribute('aria-pressed',on?'true':'false');btn.title=on?'取消收藏':'收藏到主页';btn.setAttribute('aria-label',btn.title);const use=btn.querySelector('use');if(use)use.setAttribute('href',on?'#i-star-fill':'#i-star');}
  function syncStars(){syncFavButton();document.querySelectorAll('[data-star]').forEach(node=>{const on=state.favorites.has(node.dataset.star);node.classList.toggle('on',on);node.title=on?'取消收藏':'收藏到主页';const use=node.querySelector('use');if(use)use.setAttribute('href',on?'#i-star-fill':'#i-star');});}
  // ---- 改 tmux 会话名 / 窗口名（窗口名就是侧栏显示的那个名字）
  async function renameTmuxSession(host,session){
    if(!session)return;
    const answer=await fileDialog({title:'改会话名',message:`${session} → 新的会话名（字母/数字/点/横线/下划线，最长 64）`,
      input:true,value:session,confirmLabel:'改名'});
    if(!answer.ok||!answer.value||answer.value===session)return;
    try{
      const done=await api('/api/sessions/rename',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({host:host||'localhost',session,name:answer.value})});
      toast(`会话已改成 ${done.session}`);
      // 会话名变了，pane id 不变；重拉一次树即可
      await loadPanes();
    }catch(error){toast(`改名失败：${error.message}`,4200);}
  }
  async function renameTmuxWindow(paneId,current){
    if(!paneId)return;
    const answer=await fileDialog({title:'改窗口名',message:'这个名字会出现在侧栏和 tmux 状态栏上',
      input:true,value:current||'',confirmLabel:'改名'});
    if(!answer.ok||!answer.value)return;
    try{
      const done=await api(`/api/panes/${encodeURIComponent(paneId)}/rename`,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({name:answer.value})});
      toast(`窗口已改名为 ${done.window}`);
      await loadPanes();
    }catch(error){toast(`改名失败：${error.message}`,4200);}
  }

  async function loadFavorites(){try{const d=await api('/api/home/favorites');state.favorites=new Set(d.favorites||[]);syncStars();if(window.TmuxRecents)TmuxRecents.setFavorites([...state.favorites]);}catch{}}
  async function toggleFavorite(id){const on=state.favorites.has(id);try{if(on){await api(`/api/home/favorites/${encodeURIComponent(id)}`,{method:'DELETE'});state.favorites.delete(id);}else{await api('/api/home/favorites',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pane:id})});state.favorites.add(id);}syncStars();if(window.TmuxRecents)TmuxRecents.setFavorites([...state.favorites]);}catch(e){showError(e.message);}}
  function nativeHermesNotice(signal){
    return signal?.source==='hermes_clarify' || signal?.kind==='notice' &&
      signal.options?.some(o=>['需要回答问题','等待操作审批'].includes(o.label));
  }
  function syncNativeForm(poll=false){
    const pane=state.mode==='chat' && state.panes.find(p=>p.id===state.selected);
    const signal=pane && state.needsInput?.get(pane.id);
    const native=nativeHermesNotice(signal);
    const isHermes=pane && (pane.provider==='hermes' || pane.command==='hermes' || native || state.transcriptProvider==='hermes');
    const probe=isHermes && state.nativeProbe?.has(pane.id);
    window.TmuxNativeForm?.sync(isHermes ? pane.id : '', native ? signal : null, poll, probe);
  }
  function currentInteraction(){
    const status=state.needsInput?.get(state.selected);
    if(nativeHermesNotice(status)){
      const pane=state.panes.find(p=>p.id===state.selected);
      if(pane?.provider==='hermes' || pane?.command==='hermes')return null;
    }
    if(status?.kind==='notice')return status;
    return (state.questionPane===state.selected&&state.questionNotice)||status;
  }
  function syncQuestionNotice(id,data){
    if(data.unchanged&&!Object.hasOwn(data,'open_questions'))return;
    const questions=data.open_questions||[];
    state.questionPane=id;
    state.questionNotice=questions.length?{
      kind:'notice',title:'Codex 提问',hint:'会话记录中还没有对应的工具结果，可展开实时作答。',
      read_only:true,revision:JSON.stringify(questions.map(q=>q.id)),
      options:questions.map(q=>({label:q.text}))
    }:null;
    window.TmuxPrompt?.showStatus(currentInteraction());
  }
  async function refreshPaneStates(){const now=Date.now();if(now-(state.stateAt||0)<9000)return;state.stateAt=now;try{const d=await api('/api/panes/status');state.paneState=new Map((d.panes||[]).map(p=>[p.id,p.status]));const needs=new Map((d.panes||[]).filter(p=>p.needs_input&&p.prompt).map(p=>[p.id,p.prompt]));const changed=needs.size!==(state.needsInput?state.needsInput.size:0)||[...needs.keys()].some(id=>!state.needsInput||!state.needsInput.has(id));state.needsInput=needs;state.nativeProbe=new Set((d.panes||[]).filter(p=>p.native_probe).map(p=>p.id));syncNativeForm(true);if(state.mode==='chat')window.TmuxPrompt?.showStatus(currentInteraction());applyPaneStates();if(changed)renderList();syncProgress();}catch{}}
  function applyPaneStates(){if(window.TmuxRecents)TmuxRecents.setStates(state.paneState);document.querySelectorAll('[data-pane]').forEach(button=>{const dot=button.querySelector('.pane-state');if(!dot)return;const status=state.paneState.get(button.dataset.pane)||'unknown',name='pane-state '+status;if(dot.className!==name){dot.className=name;dot.title=status==='idle'?'空闲，可接收中继':status==='busy'?'正在运行':'状态未知';}});}
  function formatSize(bytes){if(!bytes)return '0 B';const units=['B','KB','MB','GB','TB'];let value=bytes,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++;}return (index?(value<10?value.toFixed(1):Math.round(value)):value)+' '+units[index];}
  function setFileStatus(message){$('file-status').textContent=message||'拖拽文件到此处上传';}
  // per-pane sidebar memory: only consulted when 设置 → 侧栏 打开了「按 pane 记住」
  const SIDEBAR_MEMORY_KEY='tmux-sidebar-memory';
  const FILES_VIEW_KEY='tmux-files-view';
  function filesMemoryOn(){try{return localStorage.getItem(SIDEBAR_MEMORY_KEY)==='pane';}catch{return false;}}
  function readFilesView(pane){try{const all=JSON.parse(localStorage.getItem(FILES_VIEW_KEY)||'{}');const entry=all[pane];return entry&&typeof entry.open==='boolean'?entry:null;}catch{return null;}}
  function writeFilesView(){
    if(!filesMemoryOn()||!state.selected)return;
    try{
      const all=JSON.parse(localStorage.getItem(FILES_VIEW_KEY)||'{}');
      all[state.selected]={open:!!state.filesMode,path:state.filesPath||''};
      const panes=Object.keys(all);
      // 只留最近写入的 60 个 pane，避免这个映射无限长大
      if(panes.length>60){for(const key of panes.slice(0,panes.length-60))delete all[key];}
      localStorage.setItem(FILES_VIEW_KEY,JSON.stringify(all));
    }catch{}
  }
  function syncFilesPanel(){const panel=$('file-panel');panel.hidden=!state.filesMode;$('pane-list').hidden=state.filesMode;$('files-toggle').classList.toggle('active',state.filesMode);$('files-toggle').title=state.filesMode?'隐藏文件面板':'显示文件面板';if(!state.filesMode){setFileStatus('');}writeFilesView();}
  async function openFiles(path){if(!state.selected){setFileStatus('先选择一个 pane');return;}state.filesMode=true;syncFilesPanel();setFileStatus('读取目录…');const query=[`hidden=${state.filesHidden?1:0}`];if(path)query.push(`path=${encodeURIComponent(path)}`);const pane=state.selected;const seq=(state.filesSeq||0)+1;state.filesSeq=seq;try{const d=await api(`/api/panes/${encodeURIComponent(pane)}/files?${query.join('&')}`);if(!state.filesMode||pane!==state.selected||seq!==state.filesSeq)return;state.filesPane=pane;state.filesData=d;state.filesPath=d.path;state.filesPicked.clear();writeFilesView();showPath(d.path);syncFileSelection();const owner=state.hosts.find(h=>(h.panes||[]).some(x=>x.id===pane));$('file-host').textContent=owner?owner.name:pane.includes('~')?'remote':'localhost';$('file-host').hidden=false;renderFiles();syncFileSelection();setFileStatus(`${d.entries.length} 项${d.truncated?'（已截断）':''} · 拖拽文件可上传`);}catch(e){setFileStatus(e.message);}}
  function showPath(path){const field=$('file-path');field.value=path||'';field.title=path||'';field.scrollLeft=field.scrollWidth;}
  function togglePick(path){if(!path)return;if(state.filesPicked.has(path))state.filesPicked.delete(path);else state.filesPicked.add(path);state.filesAnchor=path;syncFileSelection();}
  function selectRange(path){const entries=(state.filesData&&state.filesData.entries)||[];const order=entries.map(entry=>entry.path);const anchor=order.indexOf(state.filesAnchor);const target=order.indexOf(path);if(anchor<0||target<0){togglePick(path);return;}const [from,to]=anchor<target?[anchor,target]:[target,anchor];for(let index=from;index<=to;index+=1)state.filesPicked.add(order[index]);state.filesAnchor=path;syncFileSelection();}
  function syncFileSelection(){const picked=state.filesPicked;const download=$('file-download');if(download)download.title=picked.size?`打包下载选中的 ${picked.size} 项`:'打包下载当前目录';const total=((state.filesData&&state.filesData.entries)||[]).length;const all=total>0&&picked.size===total;$('file-pick-all').hidden=!picked.size;$('file-pick-all').textContent=all?'取消全选':'全选';$('file-pick-none').hidden=!picked.size||all;$('file-zip').hidden=!picked.size;const renameBtn=$('file-rename');if(renameBtn)renameBtn.hidden=picked.size!==1;const deleteBtn=$('file-delete');if(deleteBtn)deleteBtn.hidden=!picked.size;const zip=$('file-zip');if(zip){zip.disabled=!picked.size;zip.textContent=picked.size?`下载 zip (${picked.size})`:'下载 zip';}document.querySelectorAll('.file-row[data-entry]').forEach(row=>{const on=picked.has(row.dataset.entry);row.classList.toggle('picked',on);const box=row.querySelector('.file-check');if(box)box.checked=on;});}
  function renderFiles(){const data=state.filesData;if(!data)return;$('file-list').innerHTML=(data.parent?`<button class="file-row" data-dir="${esc(data.parent)}"><span class="file-icon dir"><svg class="ico"><use href="#i-parent"/></svg></span><span class="file-name dir">..</span></button>`:'')+data.entries.map(entry=>{const isDir=entry.type==='dir';return `<button class="file-row${state.filesPicked.has(entry.path)?' picked':''}" data-entry="${esc(entry.path)}" data-type="${esc(entry.type)}"><input class="file-check" type="checkbox" tabindex="-1" aria-label="选择 ${esc(entry.name)}"><span class="file-icon${isDir?' dir':''}"><svg class="ico"><use href="#i-${isDir?'folder':(entry.type==='link'?'link':'file')}"/></svg></span><span class="file-name${isDir?' dir':''}">${esc(entry.name)}</span><span class="file-meta">${isDir?'':formatSize(entry.size)}</span></button>`;}).join('');}
  function downloadFile(path){const href=`/api/panes/${encodeURIComponent(state.selected)}/files/download?path=${encodeURIComponent(path)}`;
    const name=String(path||'').split('/').filter(Boolean).pop()||'文件';
    // 移动端必须先确认：iOS 直接打开下载链接会跳进没返回按钮的预览
    const ask=TmuxDownload.maybe(href,{name,meta:path});
    setFileStatus(`下载 ${name}`);return ask;}
  async function downloadArchive(paths,label){if(!paths.length||!state.selected)return;const button=$('file-zip');if(button)button.disabled=true;setFileStatus(`打包 ${label}…`);try{
    // 先看能不能走直连（TS/局域网）：让浏览器直接下 zip，不让整个包绕一遍 Cloudflare
    const direct=(window.TmuxTransfer&&TmuxTransfer.zipHref)?await TmuxTransfer.zipHref({pane:state.selected,base:state.filesPath||'',paths:paths}).catch(()=>null):null;
    if(direct){TmuxDownload.maybe(direct,{name:`${((state.filesPath||'files').split('/').filter(Boolean).pop())||'files'}.zip`});setFileStatus(`已交给直连下载：${label}`);return;}
    const response=await fetch(`/api/panes/${encodeURIComponent(state.selected)}/files/zip`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base:state.filesPath||'',paths:paths})});if(!response.ok){let detail=`HTTP ${response.status}`;try{detail=(await response.json()).detail||detail;}catch{}throw new Error(detail);}const blob=await response.blob();const stem=((state.filesPath||'files').split('/').filter(Boolean).pop())||'files';const url=URL.createObjectURL(blob);TmuxDownload.maybe(url,{name:`${stem}.zip`,meta:formatSize(blob.size)});setTimeout(()=>URL.revokeObjectURL(url),60000);setFileStatus(`已打包 ${label} · ${formatSize(blob.size)}`);}catch(e){setFileStatus(`打包失败：${e.message}`);}finally{if(button)button.disabled=false;syncFileSelection();}}
  async function uploadFiles(files,directory){if(!files.length||!state.selected)return;const pane=state.selected;for(const file of files){setFileStatus(`上传 ${file.name}…`);try{if(!window.TmuxUpload)throw new Error('上传模块未加载');await TmuxUpload.upload({pane,dir:directory,name:file.name,file,onProgress:(done,total)=>setFileStatus(`上传 ${file.name} ${Math.round(done/total*100)}%`)});}catch(e){setFileStatus(`${file.name}: ${e.message}`);return;}}setFileStatus(`已上传 ${files.length} 个文件`);openFiles(directory);}
  // ---- 文件面板：重命名 / 删除 ----
  function fileBase(path){return String(path||'').split('/').filter(Boolean).pop()||String(path||'');}
  function fileEntry(path){const entries=(state.filesData&&state.filesData.entries)||[];return entries.find(entry=>entry.path===path)||null;}

  function fileDialog(options){
    return new Promise(resolve=>{
      const box=$('file-dialog'),input=$('fd-input'),recRow=$('fd-check'),recBox=$('fd-recursive'),ok=$('fd-ok');
      $('fd-title').textContent=options.title||'';
      const names=(options.names||[]).filter(Boolean);
      $('fd-msg').innerHTML=names.length
        ? `<strong>${names.map(esc).join('、')}</strong>${options.warning?`<span class="fd-warn">${esc(options.warning)}</span>`:''}`
        : esc(options.message||'');
      $('fd-msg').hidden=!options.message && !names.length;
      input.hidden=!options.input;
      if(options.input)input.value=options.value||'';
      recRow.hidden=!options.recursive;
      recBox.checked=!!options.recursive;
      ok.textContent=options.confirmLabel||'确定';
      ok.className='dl-btn '+(options.danger?'danger':'primary');
      box.hidden=false;requestAnimationFrame(()=>box.classList.add('on'));
      const finish=accepted=>{
        box.hidden=true;box.classList.remove('on');
        box.onclick=null;box.onkeydown=null;ok.onclick=null;
        box.querySelector('[data-fd="cancel"]').onclick=null;
        resolve(accepted?{ok:true,value:input.value.trim(),recursive:recBox.checked}:{ok:false});
      };
      box.querySelector('[data-fd="cancel"]').onclick=()=>finish(false);
      ok.onclick=()=>finish(true);
      box.onclick=event=>{if(event.target===box)finish(false);};
      box.onkeydown=event=>{
        if(event.key==='Escape'){event.preventDefault();finish(false);}
        else if(event.key==='Enter'){event.preventDefault();finish(true);}
      };
      setTimeout(()=>{
        if(options.input){
          input.focus();
          const dot=input.value.lastIndexOf('.');           // 只选主名，扩展名留着
          const end=dot>0?dot:input.value.length;
          try{input.setSelectionRange(0,end);}catch{}
        }else ok.focus();
      },30);
    });
  }

  async function renameFile(path){
    if(!path||!state.selected)return false;
    const before=fileBase(path);
    const answer=await fileDialog({title:'重命名',message:path,input:true,value:before,confirmLabel:'重命名'});
    if(!answer.ok)return false;
    const name=answer.value;
    if(!name||name===before)return false;
    try{
      const response=await fetch(`/api/panes/${encodeURIComponent(state.selected)}/files/rename`,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({path,name})});
      const data=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);
      setFileStatus(`已重命名 → ${data.name||name}`);
      state.filesPicked.delete(path);
      await openFiles(state.filesPath);
      return true;
    }catch(error){setFileStatus(`重命名失败：${error.message}`);return false;}
  }

  async function deleteFiles(paths){
    const list=(paths||[]).filter(Boolean);
    if(!list.length||!state.selected)return false;
    const entries=list.map(fileEntry);
    const hasDir=entries.some(entry=>entry&&entry.type==='dir');
    const names=list.map(fileBase);
    const preview=names.slice(0,5).join('、')+(names.length>5?` 等 ${names.length} 项`:'');
    const answer=await fileDialog({title:names.length>1?`删除 ${names.length} 项`:'删除',
      names:names.length>5?[...names.slice(0,5),`等 ${names.length} 项`]:names,
      message:names.slice(0,5).join('、'),warning:'删除后无法恢复。',
      confirmLabel:'删除',danger:true,recursive:hasDir});
    if(!answer.ok)return false;
    try{
      const response=await fetch(`/api/panes/${encodeURIComponent(state.selected)}/files/delete`,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:list,recursive:answer.recursive})});
      const data=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);
      setFileStatus(`已删除 ${(data.deleted||[]).length} 项`);
      list.forEach(path=>state.filesPicked.delete(path));
      await openFiles(state.filesPath);
      return true;
    }catch(error){setFileStatus(`删除失败：${error.message}`);return false;}
  }

  let fileMenuTarget=null;
  function hideFileMenu(){const menu=$('file-menu');if(menu)menu.hidden=true;fileMenuTarget=null;}
  function showFileMenu(x,y,path){
    const menu=$('file-menu');if(!menu)return;
    const entry=fileEntry(path)||{type:'file',path,name:fileBase(path)};
    fileMenuTarget=entry;
    menu.querySelector('[data-fm="open"]').hidden=entry.type!=='dir';
    menu.querySelector('[data-fm="download"]').textContent=entry.type==='dir'?'打包下载':'下载';
    menu.hidden=false;
    const width=menu.offsetWidth||140,height=menu.offsetHeight||132;
    menu.style.left=`${Math.round(Math.max(6,Math.min(x,window.innerWidth-width-8)))}px`;
    menu.style.top=`${Math.round(Math.max(6,Math.min(y,window.innerHeight-height-8)))}px`;
  }

  function setupFileMenu(){
    const list=$('file-list'),menu=$('file-menu');
    if(!list||!menu)return;
    list.addEventListener('contextmenu',event=>{
      const row=event.target.closest('.file-row[data-entry]');
      if(!row)return;
      event.preventDefault();
      showFileMenu(event.clientX,event.clientY,row.dataset.entry);
    });
    // 触摸：长按 520ms 弹同一个菜单（移动端没有右键）
    let timer=0,origin=null;
    list.addEventListener('pointerdown',event=>{
      if(event.pointerType==='mouse')return;
      const row=event.target.closest('.file-row[data-entry]');
      if(!row)return;
      origin={x:event.clientX,y:event.clientY,path:row.dataset.entry};
      clearTimeout(timer);
      timer=setTimeout(()=>{if(origin)showFileMenu(origin.x,origin.y,origin.path);origin=null;},520);
    },{passive:true});
    const stop=event=>{
      if(event.type==='pointermove'&&origin&&Math.hypot(event.clientX-origin.x,event.clientY-origin.y)<8)return;
      clearTimeout(timer);
      if(event.type!=='pointermove')origin=null;
    };
    ['pointerup','pointercancel','pointermove'].forEach(type=>list.addEventListener(type,stop,{passive:true}));
    menu.addEventListener('click',event=>{
      const button=event.target.closest('[data-fm]');
      if(!button||!fileMenuTarget)return;
      const entry=fileMenuTarget;
      hideFileMenu();
      const action=button.dataset.fm;
      if(action==='open')openFiles(entry.path);
      else if(action==='download'){
        if(entry.type==='dir')downloadArchive([entry.path],entry.name||fileBase(entry.path));
        else downloadFile(entry.path);
      }else if(action==='rename')renameFile(entry.path);
      else if(action==='delete')deleteFiles([entry.path]);
    });
    document.addEventListener('pointerdown',event=>{
      if(!menu.hidden&&!event.target.closest('#file-menu'))hideFileMenu();
    },true);
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!menu.hidden)hideFileMenu();});
    list.addEventListener('keydown',event=>{
      const row=event.target.closest('.file-row[data-entry]');
      const picked=[...state.filesPicked];
      const focused=row&&row.dataset.entry;
      if(event.key==='F2'){
        const path=picked.length===1?picked[0]:focused;
        if(path){event.preventDefault();renameFile(path);}
      }else if(event.key==='Delete'){
        const paths=picked.length?picked:(focused?[focused]:[]);
        if(paths.length){event.preventDefault();deleteFiles(paths);}
      }
    });
  }

  // ---- 侧栏悬停预览：和 home 卡片共用 /assets/pane-preview.js + /api/home 的数据
  // 会话树的行和「最近」的行都绑上；手机（没有 hover）模块本身不绑。
  const previewEntries=new Map();
  const forceSidebarPreview=new URLSearchParams(location.search).has('preview');
  function previewDelay(){
    try{
      const raw=localStorage.getItem('tmux-preview-delay');
      if(raw===null||raw==='')return 2000;
      if(raw==='off')return null;                     // 设置里关掉
      const seconds=Number(raw);                      // 设置里存的是秒：1/2/3
      return Number.isFinite(seconds)&&seconds>=0?seconds*1000:2000;
    }catch{return 2000;}
  }
  async function loadPreviewEntries(){
    if(document.hidden||previewDelay()===null)return;
    try{
      const board=await api('/api/home');
      previewEntries.clear();
      for(const entry of [...(board.favorites||[]),...(board.recent||[])]){
        if(!entry||!entry.pane)continue;
        // 收藏条目带 preview/agent_preview，「最近」条目只有标题：别被后到的空条目覆盖
        const previous=previewEntries.get(entry.pane);
        const richer=previous&&(previous.agent_preview||previous.preview);
        if(richer&&!(entry.agent_preview||entry.preview))continue;
        previewEntries.set(entry.pane,entry);
      }
    }catch{/* 预览是锦上添花，取不到就算了 */}
  }
  function setupPanePreview(){
    if(!window.TmuxPanePreview)return;
    TmuxPanePreview.mount({
      force:forceSidebarPreview,
      data:()=>previewEntries,
      canShow:pane=>pane!==state.selected,
      delay:previewDelay,
      render:text=>markdown(text),
      onChat:pane=>{selectPane(pane);if(isMobile())closeSidebar();},
      onFull:pane=>{location=`/grid?add=${encodeURIComponent(pane)}`;},
      // 会话树里大多数 pane 不在 /api/home 的收藏/最近里：悬停时按需取最后几条消息
      fetch:pane=>api(`/api/panes/${encodeURIComponent(pane)}/transcript?limit=8`)
        .then(data=>TmuxPanePreview.previewFromTranscript(data)),
    });
    if(!TmuxPanePreview.hoverable)return;          // 手机 web 默认关闭（bind 也会再看一次）
    TmuxPanePreview.bind($('pane-list'),{selector:'[data-pane]',paneOf:element=>element.dataset.pane});
    // 账号级偏好（设置面板里改的）覆盖本浏览器一次，之后每页都读 localStorage
    api('/api/prefs').then(data=>{
      const stored=(data&&data.prefs||{}).preview;
      if(stored==='off'||stored==='1'||stored==='2'||stored==='3'){
        try{localStorage.setItem('tmux-preview-delay',stored);}catch{}
      }
    }).catch(()=>{}).then(loadPreviewEntries);
    setInterval(loadPreviewEntries,60_000);
    document.addEventListener('visibilitychange',loadPreviewEntries);
  }

  function setupRecentPreview(){
    // 「最近」块是 recents.js 挂载时生成的，这里等它出现再绑（同一份面板逻辑）
    if(!window.TmuxPanePreview||!TmuxPanePreview.hoverable)return;
    const list=$('recent-list');
    // steadyDelay：最近的行是一行里好几个小块（编号/圆点/标题/时间/动作），
    // 鼠标从编号滑到标题会连跨几个子元素；不加这个，每次跨界都重计延时，
    // 只有把鼠标停在某一个子元素上满 2 秒才出预览。会话树那边保持原样。
    if(list)TmuxPanePreview.bind(list,{selector:'.recent-row',paneOf:element=>element.dataset.row,steadyDelay:true});
  }


  // ---- Git 面板：改动 / 检查点 / 回滚 / worktree -------------------------
  // 后端在 /api/panes/{pane}/git*（见 docs/git-workflow.md）。这里只负责显示和按钮，
  // 所有判断都以服务端返回为准：联邦窗格没有 shell，服务端会回 501 并带一句人话。
  function gitTime(iso){const d=new Date(iso);if(Number.isNaN(d.getTime()))return iso||'';
    const pad=v=>String(v).padStart(2,'0');
    return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;}
  function gitUrl(suffix){return `/api/panes/${encodeURIComponent(state.selected)}/git${suffix||''}`;}
  function renderGitPanel(data){
    const panel=$('git-panel');if(!panel)return;
    const title=$('gp-title'),sub=$('gp-sub');
    $('gp-checkpoint').disabled=!data.repo;$('gp-worktree').disabled=!data.repo;
    if(!data.repo){title.textContent='Git';sub.textContent='这个目录不是 git 仓库';}
    else{
      title.textContent=`Git · ${data.branch||'detached'}`;
      const count=(data.changes||[]).length;
      sub.textContent=(count?`${count} 个文件有改动`:'工作区干净')+` · ${data.root}`;
      sub.title=data.root||'';
    }
    const message=$('gp-message');
    if(message&&data.repo&&!message.dataset.touched){message.value=data.suggested_message||'';}
    $('gp-branch').textContent=data.repo&&data.branch?`· ${data.branch}`:'';
    const changes=$('gp-changes'),checkpoints=$('gp-checkpoints');
    $('gp-changes-count').textContent=data.repo?`(${(data.changes||[]).length})`:'';
    if(!data.repo){changes.innerHTML='<li class="gp-empty">不是仓库，没什么可看的</li>';}
    else if(!(data.changes||[]).length){changes.innerHTML='<li class="gp-empty">工作区干净</li>';}
    else{
      changes.innerHTML=data.changes.map(item=>
        `<li data-gp-path="${esc(item.path)}" title="看这个文件的 diff">`
        +`<span class="gp-code">${esc(item.code)}</span>`
        +`<span class="gp-path">${esc(item.path)}</span></li>`).join('');
    }
    const list=data.checkpoints||[];
    if(!list.length){checkpoints.innerHTML='<li class="gp-empty">还没有检查点</li>';}
    else{
      checkpoints.innerHTML=list.map(item=>
        `<li><span class="gp-when">${esc(gitTime(item.at))}</span>`
        +`<span class="gp-path" title="${esc(item.ref)}">${esc(item.sha)}</span>`
        +`<button class="chip-btn" type="button" data-gp-restore="${esc(item.ref)}" title="把工作区退回到这个检查点">回滚</button></li>`).join('');
    }
  }
  async function loadGitPanel(){
    if(!state.selected)return;
    try{renderGitPanel(await api(gitUrl()));}
    catch(error){renderGitPanel({repo:false});$('gp-sub').textContent=error.message||'取不到 git 状态';}
  }
  async function openGitPanel(){
    if(!state.selected){toast('先选一个窗格');return;}
    $('git-panel').hidden=false;
    $('gp-diff-wrap').hidden=true;
    $('gp-commit-result').hidden=true;
    if($('gp-message'))delete $('gp-message').dataset.touched;
    $('gp-changes').innerHTML='<li class="gp-empty">加载中…</li>';
    $('gp-checkpoints').innerHTML='<li class="gp-empty">加载中…</li>';
    await loadGitPanel();
  }
  async function gpCheckpoint(){
    try{
      const result=await api(gitUrl('/checkpoint'),
        {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:''})});
      toast(result.created?`已建立检查点 ${String(result.ref).split('/').pop()}`:result.detail,3600);
      loadGitPanel();
    }catch(error){toast(error.message||'建立检查点失败',4000);}
  }
  function gpCommitResult(result){
    const box=$('gp-commit-result');box.hidden=false;
    const bits=[];
    if(result.sha)bits.push(`已提交 ${result.sha}`);
    if(result.pushed)bits.push('已推送');
    if(result.pr_url)bits.push('PR 已开');
    const head=bits.length?bits.join(' · '):'什么都没做';
    box.innerHTML=esc(head)+(result.pr_url?` · <a href="${esc(result.pr_url)}" target="_blank" rel="noopener noreferrer">打开 PR</a>`:'')
      +(result.detail?`<br><span class="gp-result-detail">${esc(result.detail)}</span>`:'');
    box.className='gp-result'+(result.exit?' bad':'');
  }
  async function gpCommit(push, pr){
    if(!state.selected)return;
    const message=($('gp-message').value||'').trim();
    const button=$(pr?'gp-commit-pr':push?'gp-commit-push':'gp-commit');
    const label=button.textContent;button.disabled=true;button.textContent='提交中…';
    try{
      const result=await api(gitUrl('/commit'),{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message,push:!!push||!!pr,pr:!!pr})});
      gpCommitResult(result);
      await loadGitPanel();
      if(state.filesMode&&state.filesPath)openFiles(state.filesPath);
    }catch(error){
      $('gp-commit-result').hidden=false;$('gp-commit-result').className='gp-result bad';
      $('gp-commit-result').textContent=error.message||'提交失败';
    }finally{button.disabled=false;button.textContent=label;}
  }
  async function gpWorktree(){
    const name=prompt('给这份 worktree 起个名字（会建分支 wt/<名字>）','task');
    if(!name)return;
    try{
      const result=await api(gitUrl('/worktree'),
        {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
      toast(`worktree：${result.path}（分支 ${result.branch}）`,6000);
      loadGitPanel();
    }catch(error){toast(error.message||'worktree 创建失败',4000);}
  }
  async function gpDiff(base, path){
    const wrap=$('gp-diff-wrap');wrap.hidden=false;
    $('gp-diff-base').textContent=[base||'(工作区 vs HEAD)',path].filter(Boolean).join(' · ');
    $('gp-diff').textContent='加载中…';
    try{
      const params=[];if(base)params.push(`base=${encodeURIComponent(base)}`);
      if(path)params.push(`path=${encodeURIComponent(path)}`);
      const data=await api(gitUrl(`/diff${params.length?'?'+params.join('&'):''}`));
      const body=String(data.patch||'').trim();
      $('gp-diff').textContent=[data.stat,body].filter(Boolean).join('\n\n')
        ||'没有已跟踪文件的差异（未跟踪文件不在 git diff 里，用文件面板看）';
    }catch(error){$('gp-diff').textContent=error.message||'取不到 diff';}
  }
  async function gpRestore(ref){
    if(!confirm(`回滚到检查点 ${String(ref).split('/').pop()}？\n检查点之后新增的未跟踪文件不会被删除。`))return;
    try{
      const data=await api(gitUrl('/restore'),
        {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ref})});
      const extra=(data.untracked||[]).length?`（${data.untracked.length} 个未跟踪文件保留）`:'';
      toast(data.matches?`已回滚到这个检查点${extra}`:`回滚没完全对上：还有 ${(data.remaining||[]).length} 个文件有差异`,5000);
      loadGitPanel();
      if(state.filesMode&&state.filesPath)openFiles(state.filesPath);
    }catch(error){toast(error.message||'回滚失败',4000);}
  }

  // ---- 排队：agent 正忙时把消息存到服务端，这一轮结束自动发 ---------------
  // 后端是 /api/panes/{pane}/queue（message_queue.py）：持久化，重启不丢；
  // 模式 mode='queue' 等空闲再发，'steer' 先按中断键再发。
  const queueState={items:[],timer:null};
  function queueUrl(suffix){return `/api/panes/${encodeURIComponent(state.selected)}/queue${suffix||''}`;}
  function renderQueueStrip(){
    const strip=$('queue-strip');if(!strip)return;
    const items=queueState.items||[];
    strip.hidden=!items.length;
    if(!items.length){strip.textContent='';return;}
    strip.innerHTML=items.map(item=>
      `<span class="qs-item${item.state==='failed'?' failed':''}" title="${esc(item.error||'')}">`
      +`<span class="qs-text">${esc(String(item.text||'').replace(/\s+/g,' ').slice(0,80))}</span>`
      +(item.state==='pending'
        ? `<button type="button" class="qs-btn" data-queue-steer="${esc(item.id)}" title="${item.mode==='steer'?'打断并立刻发（已是这个模式）':'改成打断并立刻发'}">${item.mode==='steer'?'⚡':'⇢⚡'}</button>`
        : `<span class="qs-state">${item.state==='failed'?'发送失败':''}</span>`)
      +`<button type="button" class="qs-btn" data-queue-cancel="${esc(item.id)}" title="取消这条">×</button></span>`).join('');
  }
  async function loadQueue(){
    if(!state.selected){queueState.items=[];renderQueueStrip();return;}
    try{const data=await api(queueUrl());queueState.items=data.items||[];}
    catch{queueState.items=[];}
    renderQueueStrip();
  }
  function scheduleQueuePoll(ms){
    clearTimeout(queueState.timer);
    queueState.timer=setTimeout(()=>{if(state.selected&&!document.hidden)loadQueue().then(()=>scheduleQueuePoll(4000));},ms);
  }
  async function enqueueMessage(text){
    const data=await api(queueUrl(),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,submit:true,mode:'queue'})});
    queueState.items=(await api(queueUrl())).items||[];
    renderQueueStrip();scheduleQueuePoll(2000);
    return data;
  }
  async function setQueueMode(itemId,mode){
    try{await api(queueUrl(`/${encodeURIComponent(itemId)}/mode`),{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'',mode})});
      await loadQueue();}
    catch(error){toast(error.message||'改不了这条的发送方式',3600);}
  }
  async function cancelQueueItem(itemId){
    try{await api(queueUrl(`/${encodeURIComponent(itemId)}`),{method:'DELETE'});await loadQueue();}
    catch(error){toast(error.message||'取消失败',3600);}
  }
  // 测试钩子：队列的读取/渲染都在闭包里，外面点不到
  window.TmuxQueue={load:()=>loadQueue(),state:queueState,render:renderQueueStrip};
  function setupQueueStrip(){
    const strip=$('queue-strip');if(!strip)return;
    strip.addEventListener('click',event=>{
      const cancel=event.target.closest('[data-queue-cancel]');
      if(cancel){cancelQueueItem(cancel.dataset.queueCancel);return;}
      const steer=event.target.closest('[data-queue-steer]');
      if(steer)setQueueMode(steer.dataset.queueSteer,'steer');
    });
  }
  function setupGitPanel(){
    const panel=$('git-panel');if(!panel)return;
    const open=$('git-open');if(open)open.addEventListener('click',openGitPanel);
    $('gp-close').addEventListener('click',()=>{panel.hidden=true;});
    panel.addEventListener('click',event=>{if(event.target===panel)panel.hidden=true;});
    $('gp-checkpoint').addEventListener('click',gpCheckpoint);
    $('gp-worktree').addEventListener('click',gpWorktree);
    $('gp-changes').addEventListener('click',event=>{
      const row=event.target.closest('[data-gp-path]');
      gpDiff('',row?row.dataset.gpPath:'');
    });
    const message=$('gp-message');
    if(message)message.addEventListener('input',()=>{message.dataset.touched='1';});
    $('gp-commit').addEventListener('click',()=>gpCommit(false,false));
    $('gp-commit-push').addEventListener('click',()=>gpCommit(true,false));
    $('gp-commit-pr').addEventListener('click',()=>gpCommit(true,true));
    $('gp-checkpoints').addEventListener('click',event=>{
      const button=event.target.closest('[data-gp-restore]');
      if(button)gpRestore(button.dataset.gpRestore);
    });
    document.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&!panel.hidden){panel.hidden=true;}
    });
  }
  function setupFiles(){$('files-toggle').onclick=()=>{state.filesMode=!state.filesMode;syncFilesPanel();if(state.filesMode)openFiles(state.filesPane===state.selected?state.filesPath:null);};
    $('file-reload').onclick=()=>{if(state.filesMode)openFiles(state.filesPath);};
    $('file-close').onclick=()=>{state.filesMode=false;syncFilesPanel();};
    $('file-path').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();const value=$('file-path').value.trim();if(!value){showPath(state.filesPath);return;}openFiles(value);$('file-path').blur();}else if(e.key==='Escape'){showPath(state.filesPath);$('file-path').blur();}});
    $('file-path').addEventListener('focus',()=>{const field=$('file-path');field.scrollLeft=field.scrollWidth;try{field.setSelectionRange(field.value.length,field.value.length);}catch{}});
    $('file-path').addEventListener('blur',()=>{if($('file-path').value!==(state.filesPath||''))showPath(state.filesPath);});
        $('file-pick-all').onclick=()=>{const entries=(state.filesData&&state.filesData.entries)||[];const all=entries.length>0&&state.filesPicked.size===entries.length;state.filesPicked.clear();if(!all)entries.forEach(entry=>state.filesPicked.add(entry.path));syncFileSelection();};
    $('file-pick-none').onclick=()=>{state.filesPicked.clear();state.filesAnchor=null;syncFileSelection();};
    $('file-zip').onclick=()=>downloadArchive([...state.filesPicked],`${state.filesPicked.size} 项`);
    $('file-rename').onclick=()=>{const picked=[...state.filesPicked];renameFile(picked[0]||state.filesAnchor||'');};
    $('file-delete').onclick=()=>deleteFiles([...state.filesPicked]);
    setupFileMenu();
    $('file-download').onclick=()=>{const picked=[...state.filesPicked];if(picked.length){downloadArchive(picked,`${picked.length} 项`);return;}if(!state.filesPath)return;downloadArchive([state.filesPath],`${state.filesPath.split('/').filter(Boolean).pop()||'目录'}`);};
    $('file-input').onchange=e=>{const files=Array.from(e.target.files||[]);e.target.value='';if(files.length)uploadFiles(files,state.filesPath||'');};
    $('file-list').onclick=e=>{const row=e.target.closest('.file-row');if(!row)return;
      const box=e.target.closest('.file-check');
      if(box){if(box.checked)state.filesPicked.add(row.dataset.entry);else state.filesPicked.delete(row.dataset.entry);state.filesAnchor=row.dataset.entry;syncFileSelection();return;}
      if(row.dataset.dir){if(!(e.ctrlKey||e.metaKey||e.shiftKey))openFiles(row.dataset.dir);return;}
      if(e.shiftKey){selectRange(row.dataset.entry);return;}
      if(e.ctrlKey||e.metaKey){togglePick(row.dataset.entry);return;}
      if(row.dataset.type==='dir'){openFiles(row.dataset.entry);return;}
      downloadFile(row.dataset.entry);};
    const panel=$('file-panel');let depth=0;
    panel.addEventListener('dragenter',e=>{e.preventDefault();depth++;panel.classList.add('dragging');});
    panel.addEventListener('dragover',e=>{e.preventDefault();});
    panel.addEventListener('dragleave',e=>{e.preventDefault();depth=Math.max(0,depth-1);if(!depth)panel.classList.remove('dragging');});
    panel.addEventListener('drop',e=>{e.preventDefault();depth=0;panel.classList.remove('dragging');const files=Array.from(e.dataTransfer.files||[]);if(files.length)uploadFiles(files,state.filesPath||'');});}
  // Filter metadata immediately, then scan recent content in cancellable batches.
  let paneSearchQuery='';
  let contentRun=null;
  let contentTimer=0;
  const contentHits=new Map();

  function parsePaneSearchTerms(query){return String(query||'').toLowerCase().split(/\s+/).filter(Boolean);}
  function paneSearchHaystack(pane,host,prompt){
    const parts=[pane.title,pane.target,pane.command,pane.provider,pane.id,pane.path,
      pane.session,pane.window,pane.window_name,host&&host.name,host&&host.id];
    if(pane.number!=null&&pane.number!==''){parts.push(String(pane.number),(pane.prefix?pane.prefix+'#':'#')+pane.number);}
    if(prompt){parts.push(prompt.title,prompt.hint);
      for(const option of prompt.options||[]){parts.push(option.label,option.description);}
      for(const toggle of prompt.toggles||[]){parts.push(toggle.label);}}
    return parts.map(value=>value==null?'':String(value)).join(' ').toLowerCase();}
  function paneMatchesSearch(pane,host,terms,prompt){
    if(!terms||!terms.length)return true;
    const hay=paneSearchHaystack(pane,host,prompt);
    return terms.every(term=>/^#\d+$/.test(term)?term==='#'+pane.number:hay.includes(term));}
  function filterHostsForSearch(hosts,terms,needsInput,hits){
    if(!terms||!terms.length)return {hosts:hosts,matched:0};
    let matched=0;const filtered=[];
    for(const host of hosts||[]){
      const panes=(host.panes||[]).filter(pane=>{
        const prompt=needsInput&&needsInput.get?needsInput.get(pane.id):undefined;
        const ok=paneMatchesSearch(pane,host,terms,prompt)||hits?.has(pane.id);
        if(ok)matched++;
        return ok;});
      if(panes.length)filtered.push({...host,panes});}
    return {hosts:filtered,matched};}
  function treeDetails(key,label,children,kind,count,extra='',tail='',forceOpen=false){const open=(forceOpen||state.treeOpen.has(key))?' open':'';return `<details class="tree-group tree-${kind}" data-tree="${esc(key)}"${open}><summary>${extra}<span class="tree-label">${esc(label)}</span><span class="tree-count">${count}</span>${tail}</summary><div class="tree-children">${children}</div></details>`;}
  function renderList(){const terms=parsePaneSearchTerms(paneSearchQuery);const searching=terms.length>0;
    const view=searching?filterHostsForSearch(state.hosts,terms,state.needsInput,contentHits):{hosts:state.hosts,matched:0};
    const html=view.hosts.map(host=>{const sessions=new Map();for(const p of host.panes||[]){if(!sessions.has(p.session))sessions.set(p.session,new Map());const windows=sessions.get(p.session),windowKey=`${p.window}:${p.window_name||''}`;if(!windows.has(windowKey))windows.set(windowKey,[]);windows.get(windowKey).push(p);}const sessionHtml=[...sessions].map(([session,windows])=>{const windowHtml=[...windows].map(([windowKey,panes])=>{const first=panes[0],label=`${first.window}${first.window_name?': '+first.window_name:''}`,leaves=[...panes].sort((a,b)=>((state.needsInput&&state.needsInput.has(b.id)?1:0)-(state.needsInput&&state.needsInput.has(a.id)?1:0))).map(p=>`<button class="pane ${p.id===state.selected?'selected':''}${state.needsInput&&state.needsInput.has(p.id)?' needs-you':''}" data-pane="${esc(p.id)}"><div class="pane-row"><span class="dot ${p.active?'active':''}"></span><span class="pane-num">${paneLabel(p)}</span>${state.needsInput&&state.needsInput.has(p.id)?'<span class="pane-need">等你</span>':''}<span class="pane-target">pane ${esc(p.index)} · ${esc(p.command)}</span><span class="pane-star ${state.favorites.has(p.id)?'on':''}" data-star="${esc(p.id)}" role="button" tabindex="-1" title="${state.favorites.has(p.id)?'取消收藏':'收藏到主页'}"><svg class="ico"><use href="#i-${state.favorites.has(p.id)?'star-fill':'star'}"/></svg></span><span class="pane-state ${state.paneState.get(p.id)||'unknown'}"></span></div><div class="pane-title">${esc(p.title||'(无标题)')}</div><div class="pane-meta">${esc(p.path)}</div>${contentSnippet(p.id)}</button>`).join('');return treeDetails(`${host.id}/${session}/${windowKey}`,label,leaves,'window',`${panes.length}`,'',
      `<button class="tree-tool tree-rename" type="button" data-rename-window="${esc(first.id)}" data-rename-value="${esc(first.window_name||'')}" title="改窗口名" aria-label="改窗口名"><svg class="ico"><use href="#i-pencil"/></svg></button>`,searching);}).join('');
      return treeDetails(`${host.id}/${session}`,session,windowHtml,'session',`${windows.size} windows`,'',
      `<button class="tree-tool tree-rename" type="button" data-rename-session="${esc(session)}" data-rename-host="${esc(host.id)}" title="改会话名" aria-label="改会话名"><svg class="ico"><use href="#i-pencil"/></svg></button>`,searching);}).join('');const firstPath=(host.panes&&host.panes[0]&&host.panes[0].path)||'';
  return treeDetails(host.id,host.name||host.id,sessionHtml,'host',`${(host.panes||[]).length}`,
    `<span class="host-icon"><svg class="ico"><use href="#i-server"/></svg></span><span class="host-status"></span>`,
    `<button class="host-add" type="button" data-new-session="${esc(host.id)}" data-session-path="${esc(firstPath)}"`
    + ` title="在该主机新建 tmux 会话" aria-label="新建 tmux 会话"><svg class="ico"><use href="#i-plus"/></svg></button>`
    + `<button class="host-refresh" type="button" data-refresh-host="${esc(host.id)}"`
    + ` title="刷新这台主机的 pane 列表" aria-label="刷新主机"><svg class="ico"><use href="#i-refresh"/></svg></button>`,searching);}).join('');
    const countEl=$('pane-search-count');
    if(countEl){
      countEl.hidden=!searching;
      if(searching){
        const progress=contentRun?.pending?' · 正在查正文…':'';
        const unread=contentRun?.unread?' · '+contentRun.unread+' 个会话未完整搜索':'';
        countEl.textContent=`命中 ${view.matched} 个`+progress+unread;
      }
    }
    if(searching&&!view.matched){
      const label=contentRun?.pending?'正在搜索聊天记录…':contentRun?.unread?'已查内容没有匹配':'没有匹配的会话';
      if(list.dataset.sig!==label){list.dataset.sig=label;list.innerHTML='<div class="tree-empty">'+label+'</div>';}
      return;}
    // replacing an identical tree swallows clicks and resets the scroll position
    if(list.dataset.sig!==html){list.dataset.sig=html;list.innerHTML=html;}
    if(window.TmuxRecents){TmuxRecents.markActive(state.selected||null);}}
  function syncLiveButton(){const btn=$('live-toggle');btn.disabled=!state.selected;btn.title=state.livePaused?'继续实时刷新':'暂停实时刷新';
    const use=btn.querySelector('use');if(use)use.setAttribute('href',state.livePaused?'#i-play':'#i-pause');}
  function pauseLive(){if(!state.selected||state.livePaused)return;state.livePaused=true;closeConnections(true);
    $('status-dot').className='status-dot paused';$('status-text').textContent='已暂停实时刷新（点 ▶ 继续）';syncLiveButton();}
  function resumeLive(){if(!state.livePaused)return;state.livePaused=false;syncLiveButton();if(!state.selected)return;
    $('status-text').textContent='连接中…';
    // keep the frozen screen: the new attach repaints into it, so there is no black gap while connecting
    if(state.mode==='terminal')connectTerminal(state.selected,true);
    else connectTranscript(state.selected,true);}
  function closeConnections(keepTerminal){cancelScheduledTranscriptRender();stopLiveTail();state.termPending='';state.viewportFitTimers.forEach(clearTimeout);state.viewportFitTimers=[];if(state.ws){state.ws.close();state.ws=null;}if(state.termWs){state.termWs.close();state.termWs=null;}if(state.transcriptTimer){clearTimeout(state.transcriptTimer);state.transcriptTimer=null;}if(state.resizeObserver){state.resizeObserver.disconnect();state.resizeObserver=null;}if(!keepTerminal&&state.xterm){state.xterm.dispose();state.xterm=null;state.fit=null;$('terminal-host').innerHTML='';}}
  async function loadSessionAgents(hostId){const select=$('session-agent');
    select.innerHTML='<option value="">无（普通 shell）</option>';$('session-args-field').hidden=true;$('session-args').value='';
    try{const data=await api(`/api/hosts/${encodeURIComponent(hostId)}/agents`);
      for(const agent of data.agents||[]){const option=document.createElement('option');option.value=agent.name;
        option.textContent=agent.label||agent.name;option.dataset.path=agent.path||'';select.appendChild(option);}
      syncAgentHint();
      select.dataset.count=String((data.agents||[]).length);
      if(!(data.agents||[]).length){const option=document.createElement('option');option.value='';option.disabled=true;
        option.textContent='这台主机没检测到 agent';select.appendChild(option);}
    }catch(error){select.dataset.count='0';}}
  function syncAgentHint(){const select=$('session-agent');const option=select.options[select.selectedIndex];
    const path=(option&&option.dataset.path)||'';const hint=$('session-agent-hint');
    hint.textContent=path?`将运行：${option.textContent} · ${path}`:'';hint.hidden=!path;}
  function openSessionDialog(hostId,path){const host=state.hosts.find(item=>item.id===hostId);
    $('session-host-label').textContent=`主机：${host?host.name:hostId}`;$('session-name').value='';$('session-path').value=path||'';
    $('session-status').textContent='';$('session-create').disabled=false;loadSessionAgents(hostId);
    if(typeof $('session-dialog').showModal==='function')$('session-dialog').showModal();else $('session-dialog').setAttribute('open','');
    $('session-name').focus();}
  async function submitSessionForm(){const hostId=state.sessionHost||'localhost';const name=$('session-name').value.trim();
    if(!name){$('session-status').textContent='请填写会话名称';return;}
    $('session-create').disabled=true;$('session-status').textContent='正在创建会话…';
    try{const result=await api('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({host:hostId,name,path:$('session-path').value.trim(),
        agent:$('session-agent').value||'',agent_args:$('session-args').value.trim()})});
      state.treeOpen.add(hostId);if(result.session&&result.session.session)state.treeOpen.add(`${hostId}/${result.session.session}`);
      $('session-dialog').close();await loadPanes();
      const fresh=(result.panes&&result.panes[0])||null;if(fresh)selectPane(fresh.id);
      const bits=[`已创建会话 ${result.session.session}`];
      if(result.session.created_dir)bits.push('已新建目录');
      if(result.session.command)bits.push(result.session.command);
      $('status-text').textContent=bits.join(' · ');
    }catch(error){$('session-status').textContent=`创建失败：${error.message}`;}
    finally{$('session-create').disabled=false;}}
  function openPaneCloseDialog(){const id=state.selected;if(!id)return;
    const pane=state.panes.find(item=>item.id===id)||{};
    $('pane-close-sub').textContent=[pane.number?`#${pane.number}`:'',pane.target||id,
      pane.command?`· ${pane.command}`:''].filter(Boolean).join(' ');
    $('pane-close-status').textContent='';$('pane-close-confirm').disabled=false;
    if(typeof $('pane-close-dialog').showModal==='function')$('pane-close-dialog').showModal();
    else $('pane-close-dialog').setAttribute('open','');
    $('pane-close-cancel').focus();}
  // 模型选择：按 provider 分组、可折叠（Hermes 一个 config 里常有好几组）
  let modelState={groups:[],current:'',currentProvider:'',pickerOnly:false,open:new Set()};
  function modelLabel(){const name=modelState.current||'选择模型';
    return modelState.currentProvider?`${modelState.currentProvider} · ${name}`:name;}
  function renderModelMenu(){
    $('model-current').textContent=modelLabel();
    const host=$('model-groups');host.innerHTML='';
    if(modelState.nativePicker || modelState.pickerOnly){
      const button=document.createElement('button');
      button.type='button';button.className='model-item';
      button.textContent='打开原生模型菜单';
      button.onclick=()=>switchModel('','');
      host.appendChild(button);if(modelState.pickerOnly)return;}
    if(!modelState.groups.length){
      const empty=document.createElement('div');empty.className='model-empty';
      empty.textContent='这个 provider 没有返回模型列表';host.appendChild(empty);}
    for(const group of modelState.groups){
      const wrap=document.createElement('div');
      wrap.className='model-group'+(modelState.open.has(group.provider)?' open':'');
      wrap.dataset.provider=group.provider;
      const head=document.createElement('button');
      head.type='button';head.className='model-group-head';
      head.innerHTML=`<svg class="ico model-caret"><use href="#i-chevron"/></svg><span>${esc(group.provider+(modelState.source==='host_cache'?' · 主机缓存':modelState.source==='aliases'?' · 常用别名':''))}</span><span class="count">${group.models.length}</span>`;
      // 重绘会把被点的节点从文档里摘掉，若不拦住冒泡，外面那层"点别处就关"会当成点了外面
      head.onclick=event=>{event.stopPropagation();
        if(modelState.open.has(group.provider))modelState.open.delete(group.provider);
        else modelState.open.add(group.provider);renderModelMenu();};
      wrap.appendChild(head);
      const items=document.createElement('div');items.className='model-group-items';
      for(const model of group.models){
        const item=document.createElement('button');
        item.type='button';item.className='model-item'+(model===modelState.current&&group.provider===modelState.currentProvider?' is-current':'');
        item.dataset.model=model;item.dataset.provider=group.provider;item.textContent=model;
        item.onclick=event=>{event.stopPropagation();switchModel(model,group.provider);};
        items.appendChild(item);}
      wrap.appendChild(items);host.appendChild(wrap);}
    const custom=document.createElement('button');
    custom.type='button';custom.className='model-item';custom.style.paddingLeft='8px';
    custom.textContent='自定义模型 id…';custom.onclick=()=>{
      const value=(prompt('模型 id（会发给 agent 的 /model 命令）')||'').trim();
      if(value)switchModel(value,modelState.currentProvider||'');};
    host.appendChild(custom);
  }
  function filterModels(query){const q=(query||'').trim().toLowerCase();
    for(const group of $('model-groups').querySelectorAll('.model-group')){
      let shown=0;
      for(const item of group.querySelectorAll('.model-item')){
        const hit=!q||item.textContent.toLowerCase().includes(q);
        item.style.display=hit?'':'none';if(hit)shown++;}
      group.style.display=shown?'':'none';
      if(q)group.classList.toggle('open',shown>0);}}
  function openModelMenu(open){
    const menu=$('model-menu'),toggle=$('model-toggle');
    menu.hidden=!open;toggle.setAttribute('aria-expanded',open?'true':'false');
    if(open){$('model-filter').value='';filterModels('');
      menu.style.removeProperty('--menu-offset');
      const rect=menu.getBoundingClientRect();
      const offset=Math.max(8-rect.left, Math.min(0, window.innerWidth-8-rect.right));
      if(offset)menu.style.setProperty('--menu-offset', Math.round(offset)+'px');
      $('model-filter').focus({preventScroll:true});}
  }
  function applyModels(data){const current=(data.current||'').trim();
    modelState={groups:[],current,currentProvider:(data.current_provider||data.provider_label||'').trim(),
      pickerOnly:!!data.picker_only,nativePicker:!!data.native_picker,source:data.catalog_source,open:new Set()};
    if(modelState.currentProvider)modelState.open.add(modelState.currentProvider);
    const groups=(data.groups||[]).filter(group=>(group.models||[]).length);
    modelState.groups=groups.length?groups:
      (data.presets||[]).length?[{provider:modelState.currentProvider||'模型',models:data.presets}]:[];
    renderModelMenu();}
  function resetPaneModels(id){
    state.modelsToken=(state.modelsToken||0)+1;
    modelState={groups:[],current:'',currentProvider:'',pickerOnly:false,open:new Set()};
    openModelMenu(false);
    const pane=state.panes.find(item=>item.id===id);
    const name=pane?.provider||pane?.command||'';
    $('model-pick-wrap').hidden=!/^(codex|claude|hermes|kimi)(?:$|[- ])/i.test(name);
    $('model-current').textContent='选择模型';
  }
  async function loadPaneModels(id){
    if(!id)return;
    const token=++state.modelsToken;
    const host=$('model-groups');
    host.textContent='正在读取模型…';
    try{
      const data=await api('/api/panes/'+encodeURIComponent(id)+'/models');
      if(id!==state.selected||token!==state.modelsToken)return;
      applyModels(data);
      filterModels($('model-filter').value);
    }catch(error){
      if(id===state.selected&&token===state.modelsToken)host.textContent='读取失败：'+error.message;
    }
  }
  async function switchModel(model,provider){const id=state.selected;if(!id)return;
    openModelMenu(false);
    const body=model?{model,provider:provider||''}:{};
    try{const result=await api(`/api/panes/${encodeURIComponent(id)}/model`,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(id!==state.selected)return;
      if(result.command==='/model'){
        setTimeout(()=>{if(state.selected!==id||state.mode!=='chat')return;
          if($('inspect-interaction').hidden)$('chat-reply-panel').dispatchEvent(new Event('open-reply'));
          else window.TmuxPrompt?.inspect(()=>state.mode==='chat'?state.selected:null);},500);
      }
      toast(result.command==='/model'?'已在 agent 里打开模型菜单，用方向键选':`已发送：${result.command}`,2400);
    }catch(error){showError(`切换模型失败：${error.message}`);}}
  async function closeSelectedPane(){const id=state.selected;if(!id)return;
    const button=$('pane-close-confirm');button.disabled=true;$('pane-close-status').textContent='正在关闭…';
    try{const result=await api(`/api/panes/${encodeURIComponent(id)}`,{method:'DELETE'});
      $('pane-close-dialog').close();closeConnections();state.selected=null;rememberPane(null);
      $('message-shell').hidden=true;$('empty').style.display='grid';$('input').disabled=true;$('send').disabled=true;
      $('selected-number').hidden=true;
      if(!result.session_alive){$('selected-title').textContent='选择一个会话';$('selected-sub').textContent='窗格已关闭，会话也随之结束';}
      state.wantedPane=null;
      await loadPanes();
      // 刚关掉的那个 pane 不能被“自动恢复”选中：同一个会话还有别的 pane 就落到它上面
      const stale=!state.selected||state.selected===id||!state.panes.some(p=>p.id===state.selected);
      if(stale){const sibling=state.panes.find(p=>p.session===result.session&&p.id!==id);
        if(sibling)selectPane(sibling.id);
        else{state.selected=null;closeConnections();
          $('selected-title').textContent='选择一个会话';$('selected-number').hidden=true;
          $('selected-sub').textContent=result.session_alive?'窗格已关闭':'窗格已关闭，会话也随之结束';
          $('message-shell').hidden=true;$('empty').style.display='grid';
          $('input').disabled=true;$('send').disabled=true;syncFavButton();}}
      toast(`已关闭 ${result.pane}${result.session_alive?'，会话仍在':'，会话也随之结束'}`);
    }catch(error){$('pane-close-status').textContent=`关闭失败：${error.message}`;}
    finally{button.disabled=false;}}
  $('transcript').addEventListener('click',event=>{
    const resend=event.target.closest('[data-resend]');
    if(resend){event.preventDefault();resendPending(resend.dataset.resend);return;}
    const messageCopy=event.target.closest('[data-msg-copy]');
    if(messageCopy){const text=(state.messageText&&state.messageText.get(messageCopy.dataset.msgCopy))||'';copyText(text,messageCopy);return;}
    // 再次发送（借自 Open WebUI 的 regenerate/edit）：只把原文填回输入框，
    // 不自动发送——用户可改完再回车，避免对 agent 重复执行同一条指令。
    const messageResend=event.target.closest('[data-msg-resend]');
    if(messageResend){const text=(state.messageText&&state.messageText.get(messageResend.dataset.msgResend))||'';if(!text||!state.selected||input.disabled)return;input.value=text;input.dispatchEvent(new Event('input'));input.focus();input.setSelectionRange(input.value.length,input.value.length);return;}
    const localLink=event.target.closest('a[href]');
    if(localLink&&looksLikeMarkdownLink(localLink)&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey){
      event.preventDefault();openMarkdownViewer(localLink.getAttribute('href'),localLink.textContent.trim()||'');return;}
    const blockCopy=event.target.closest('[data-block-copy]');
    if(blockCopy){const wrap=blockCopy.closest('.block-wrap');if(!wrap)return;
      copyText(wrap.dataset.kind==='table'?wrap.dataset.tsv:wrap.dataset.source,blockCopy);return;}
    const blockSource=event.target.closest('[data-block-source]');
    if(blockSource){const wrap=blockSource.closest('.block-wrap');if(wrap)toggleBlockSource(wrap,blockSource);return;}});
  function paneCwd(){const pane=state.panes.find(item=>item.id===state.selected);return pane&&pane.path?pane.path.replace(/\/+$/,''):'';}
  function fileLinkHref(raw,baseCwd){const value=String(raw||'').trim();if(!value)return '';
    // real URLs and in-page anchors stay untouched
    if(/^(?:https?:|mailto:|tel:|data:|blob:|javascript:|#)/i.test(value))return value;
    if(value.startsWith('//'))return value;
    let path=value;
    if(/^file:\/\//i.test(path))path=path.slice(7).replace(/^localhost/i,'');
    // marked percent-encodes hrefs, so undo that before touching the filesystem
    try{if(/%[0-9A-Fa-f]{2}/.test(path))path=decodeURIComponent(path);}catch(_error){}
    // editors link to "file.md:3685": keep the file, drop the line/column suffix
    if(!/^[A-Za-z]:[\\/]/.test(path))path=path.replace(/^(.*?):\d+(?::\d+)?$/,(match,file)=>file||match);
    const cwd=baseCwd!==undefined?baseCwd:paneCwd();
    if(/^~\//.test(path)&&cwd)path=cwd.replace(/[^/]*$/,'')+path.slice(2);
    const absolute=path.startsWith('/')||/^[A-Za-z]:[\\/]/.test(path);
    if(!absolute){if(!cwd)return value;path=cwd+'/'+path;}
    if(!state.selected)return value;
    return `/api/panes/${encodeURIComponent(state.selected)}/files/raw?path=${encodeURIComponent(path)}`;}
  // 消息里的路径（/a/b、~/x、report.md）后处理成链接：目录 → 左侧文件面板，文件 → 下载，
  // markdown → 预览（带下载按钮）。实现在 /assets/path-links.js。
  function fileDownloadHref(path){return `/api/panes/${encodeURIComponent(state.selected)}/files/download?path=${encodeURIComponent(path)}`;}
  // 长会话一下子上百条消息，逐条扫文本太费；改成"碰到哪条处理哪条"：
  // 鼠标移入 / 手指按下 时才扫这一条，结果打上 data-paths 不重复处理。
  function enhancePathsLazy(container, force){
    if(!container||SAFE_MODE||NOLINK_MODE)return;
    if(container.dataset.pathsBound==='1'&&!force)return;
    if(container.dataset.pathsBound==='1'){eagerPass(container);return;}
    container.dataset.pathsBound='1';
    const touch=event=>{
      const message=event.target.closest&&event.target.closest('.chat-message');
      if(!message||message.dataset.paths==='1')return;
      message.dataset.paths='1';
      enhancePaths(message);
    };
    container.addEventListener('pointerover',touch,true);
    container.addEventListener('pointerdown',event=>{
      touch(event);
      // 触摸没有 hover：这一次按下去就顺手把刚生成的路径打开
      if(event.pointerType==='mouse')return;
      const {clientX,clientY}=event;
      if(window.TmuxPaths&&window.TmuxPaths.activateAt)window.TmuxPaths.activateAt(clientX,clientY,event.target.closest('.chat-message')||event.target);
    },true);
    container.addEventListener('focusin',touch,true);
    eagerPass(container);
  }

  // 最后几条不必等悬停：视频/图片预览要一打开就能看到。
  // 每次整段重渲染（轮询刷新会换掉 DOM）之后要重新跑一遍，否则新节点永远没人处理。
  function eagerPass(container){
    const run=()=>{[...container.querySelectorAll('.chat-message')].slice(-8).forEach(message=>{
      if(message.dataset.paths==='1')return;message.dataset.paths='1';enhancePaths(message);});};
    if(window.requestIdleCallback)requestIdleCallback(run,{timeout:600});else setTimeout(run,120);
  }
  function enhancePaths(root){
    if(!root||!window.TmuxPaths||!state.selected)return;
    const cwd=panePathOf(state.selected)||'';
    window.TmuxPaths.enhance(root,{
      pane:state.selected,cwd,
      onDir:path=>{openFiles(path);},
      onFile:path=>{const dir=path.replace(/[^/]*$/,'')||path;openFiles(dir);downloadFile(path);},
      onMd:path=>openMarkdownViewer(`/api/panes/${encodeURIComponent(state.selected)}/files/raw?path=${encodeURIComponent(path)}`,path.split('/').pop()),
      onPreview:path=>openMarkdownViewer(`/api/panes/${encodeURIComponent(state.selected)}/files/raw?path=${encodeURIComponent(path)}`,path.split('/').pop()),
      onMissing:path=>showError(`找不到：${path}`),
      onError:message=>showError(message),
    });
    // .plink 生成之后才轮得到视频卡片（顺序反了的话视频永远拿不到链接）
    enhanceVideos(root);
  }
  // 附件预览、票据、静态资源都是应用自己的地址，别当成"本地文件"去 rewrite
  const APP_URL_RE=/^\/(api|t|assets)\//;
  function rewriteLocalLinks(root,baseCwd){if(!root)return;
    root.querySelectorAll('a[href]').forEach(link=>{if(link.dataset.upload==='1')return;
      const href=link.getAttribute('href')||'';if(APP_URL_RE.test(href))return;
      const next=fileLinkHref(href,baseCwd);if(next===href)return;
      link.setAttribute('href',next);link.setAttribute('target','_blank');link.setAttribute('rel','noopener noreferrer');
      link.dataset.localFile='1';
      link.title=(link.title||'')+ (link.title?' · ':'') + '本地文件';});
    // images pointing at local paths would otherwise 404 against the console origin
    root.querySelectorAll('img[src]').forEach(image=>{if(image.dataset.upload==='1')return;
      const src=image.getAttribute('src')||'';if(APP_URL_RE.test(src))return;
      const next=fileLinkHref(src,baseCwd);if(next===src)return;
      image.setAttribute('src',next);image.dataset.localFile='1';image.classList.add('local-image');
      image.title=image.title||'本地图片';});
    enhanceVideos(root);}
  function enhanceVideos(root){if(window.TmuxVideo&&!SAFE_MODE&&!NOVIDEO_MODE)window.TmuxVideo.enhance(root);}
  const MD_VIEWER_EXTS=/^(md|markdown|mdx|txt|log|json|ya?ml|csv)$/i;
  function markdownViewerBase(href){try{const url=new URL(href,location.origin);
    const path=url.searchParams.get('path')||'';return path.replace(/[^/]*$/,'');}catch(_error){return '';}}
  function markdownViewerName(href){try{const url=new URL(href,location.origin);
    const path=url.searchParams.get('path')||'';return path.split('/').pop()||'文件';}catch(_error){return '文件';}}
  function looksLikeMarkdownLink(link){
    if(!link||!link.dataset.localFile)return false;
    const href=link.getAttribute('href')||'';
    if(!href.includes('/files/raw?'))return false;
    let path='';
    try{path=new URL(href,location.origin).searchParams.get('path')||'';}catch(_error){return false;}
    const ext=(path.split('.').pop()||'').toLowerCase();
    return MD_VIEWER_EXTS.test(ext);}
  async function openMarkdownViewer(href,title){
    const viewer=$('md-viewer'),body=$('md-body'),toggle=$('md-toggle');
    state.viewer={href,base:markdownViewerBase(href),source:'',path:'',rendered:false};
    $('md-title').textContent=title||markdownViewerName(href);
    $('md-path').textContent='';
    $('md-open').setAttribute('href',href);
    const path=new URL(href,location.origin).searchParams.get('path')||'';
    const downloadButton=$('md-download');
    if(downloadButton&&path){
      downloadButton.setAttribute('href',fileDownloadHref(path));
      // 移动端：点「下载」先确认，别直接跳进系统预览
      downloadButton.onclick=event=>{
        if(!TmuxDownload.isMobile())return;
        event.preventDefault();
        TmuxDownload.maybe(fileDownloadHref(path),{name:path.split('/').filter(Boolean).pop()||'文件',meta:path});
      };
    }
    toggle.hidden=false;toggle.textContent='源码';toggle.setAttribute('aria-pressed','false');
    body.className='md-body';body.innerHTML='<p class="md-hint">加载中…</p>';
    viewer.hidden=false;document.body.classList.add('viewer-open');
    try{
      const response=await fetch(href,{cache:'no-store'});
      if(!response.ok)throw new Error(response.status===404?'文件不存在':'读取失败（'+response.status+'）');
      const text=await response.text();
      const path=markdownViewerName(href);
      state.viewer.source=text;state.viewer.path=path;
      $('md-path').textContent=decodeURIComponent(new URL(href,location.origin).searchParams.get('path')||'');
      const isMarkdown=/\.(md|markdown|mdx)$/i.test(path);
      if(!isMarkdown&&text.length>400000){renderSource();return;}
      if(!isMarkdown){body.innerHTML=`<pre>${htmlEscape(text)}</pre>`;toggle.hidden=true;return;}
      if(text.length>1200000){
        body.innerHTML=`<p class="md-hint">文件较大（${Math.round(text.length/1024)} KB），默认显示源码，可切换渲染。</p><pre>${htmlEscape(text.slice(0,1200000))}</pre>`;
        state.viewer.rendered=false;toggle.textContent='渲染';toggle.setAttribute('aria-pressed','true');return;}
      renderMarkdown();
    }catch(error){
      body.innerHTML=`<p class="block-error">${htmlEscape(String(error.message||error))}</p>`;
      toggle.hidden=true;}
    function renderSource(){
      body.className='md-body source';body.textContent=state.viewer.source;
      toggle.textContent='渲染';toggle.setAttribute('aria-pressed','true');state.viewer.rendered=false;}
    function renderMarkdown(){
      body.className='md-body';
      body.innerHTML=`<article class="assistant"><div class="assistant-body">${markdown(state.viewer.source)}</div></article>`;
      enhanceBlocks(body);rewriteLocalLinks(body,state.viewer.base);
      toggle.textContent='源码';toggle.setAttribute('aria-pressed','false');state.viewer.rendered=true;
      body.scrollTop=0;}
    state.viewer.renderSource=renderSource;state.viewer.renderMarkdown=renderMarkdown;}
  function closeMarkdownViewer(){const viewer=$('md-viewer');if(!viewer||viewer.hidden)return;
    viewer.hidden=true;document.body.classList.remove('viewer-open');$('md-body').innerHTML='';state.viewer=null;}
  $('load-earlier').addEventListener('click',loadEarlier);
  $('md-viewer').addEventListener('click',event=>{
    if(event.target.closest('[data-md-close]')){closeMarkdownViewer();return;}
    // links inside a previewed document open the next document in place
    const link=event.target.closest('#md-body a[href]');
    if(link&&looksLikeMarkdownLink(link)&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey){
      event.preventDefault();openMarkdownViewer(link.getAttribute('href'),link.textContent.trim()||'');}});
  $('md-toggle').addEventListener('click',()=>{const viewer=state.viewer;if(!viewer||!viewer.renderMarkdown)return;
    viewer.rendered?viewer.renderSource():viewer.renderMarkdown();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')closeMarkdownViewer();});
  // Keep every loaded message mounted. Rich blocks fall back to readable source
  // when necessary to leave room for the rest of the page within the DOM budget.
  const TRANSCRIPT_PAGE=300,TRANSCRIPT_PAGE_MAX=500;
  function sinceSeconds(value){if(!value)return 0;let seconds=0;
    if(typeof value==='number')seconds=value>1e12?value/1000:value;
    else{const parsed=Date.parse(String(value));if(!Number.isNaN(parsed))seconds=parsed/1000;
      else{const numeric=Number(value);if(Number.isFinite(numeric))seconds=numeric;}}
    if(!seconds)return 0;const delta=Date.now()/1000-seconds;
    return delta>0&&delta<86400?Math.round(delta):0;}
  function humanElapsed(seconds){if(seconds<3)return '';if(seconds<60)return `${seconds}s`;
    if(seconds<3600)return `${Math.floor(seconds/60)}m${String(seconds%60).padStart(2,'0')}s`;
    return `${Math.floor(seconds/3600)}h${String(Math.floor((seconds%3600)/60)).padStart(2,'0')}m`;}
  function agentWorking(){
    if(!state.selected)return false;
    // plain shells have no turn to wait for; only agents get the progress line
    if(!state.transcriptProvider&&!state.waiting)return false;
    if(state.waiting)return true;
    return !!state.transcriptProvider&&!!state.paneState&&state.paneState.get(state.selected)==='busy';}
  function syncSendButton(){
    const button=$('send');if(!button)return;
    const text=input.value.trim();
    const working=state.mode==='chat'&&!!state.selected&&agentWorking();
    const stop=working&&!text;
    if(button.dataset.mode!==(stop?'stop':'send')){
      button.dataset.mode=stop?'stop':'send';
      button.innerHTML=stop?'<svg class="ico"><use href="#i-stop"/></svg>':'<svg class="ico"><use href="#i-arrow-up"/></svg>';
      button.title=stop?'停止当前推理（Esc）':'发送';
      button.setAttribute('aria-label',stop?'停止':'发送');
      button.classList.toggle('stop',stop);}
  }
  async function stopAgent(){
    const pane=state.selected;if(!pane)return;
    const button=$('send');button.disabled=true;setSendState('正在停止…');
    try{
      const result=await api(`/api/panes/${encodeURIComponent(pane)}/interrupt`,{method:'POST'});
      setSendState(result&&result.key==='C-q'?'已发送 Ctrl+Q':'已发送 Esc','ok');
    }catch(error){showError(error.message);setSendState('停止失败','bad');}
    finally{setTimeout(()=>{button.disabled=!state.selected;if(button.disabled===false)setSendState('');syncSendButton();},700);}
  }
  function syncProgress(){
    const box=$('agent-progress'),dot=$('status-dot');
    const working=state.mode==='chat'&&!!state.selected&&agentWorking();
    // the strip sits above the composer: thinking while the agent reasons, idle otherwise
    if(box){
      // 加载中优先：这时候 remote 的状态还没到，显示 idle/thinking 都是假的
      const loading=state.mode==='chat'&&!!state.selected&&loadingActive();
      box.hidden=!(state.mode==='chat'&&!!state.selected);
      box.classList.toggle('working',working||loading);
      $('agent-progress-label').textContent=loading?loadingLabel():(working?'thinking':'idle');
      $('agent-progress-elapsed').textContent=loading?loadingDetail():(working?humanElapsed(sinceSeconds(state.waitingSince)):'');
      box.title=loading?loadCard.label:'';
    }
    if(dot){
      // a paused view is not live: neutral dot, the label says why
      if(state.livePaused&&state.selected){dot.className='status-dot paused';}
      else if(working){dot.className='status-dot busy';}
      else if(state.mode==='chat'&&state.selected){dot.className='status-dot live';}
    }
    const status=$('status-text');
    if(status&&state.livePaused&&state.selected){
      status.textContent='已暂停实时刷新（点 ▶ 继续）';}
    else if(status&&state.mode==='chat'&&state.selected&&state.transcriptProvider){
      status.textContent=working?`${state.transcriptProvider} · thinking…`:`${state.transcriptProvider} transcript`;}
    syncSendButton();
    syncLiveTail();
  }
  function syncEarlierBar(){const bar=$('earlier-bar'),button=$('load-earlier');
    if(!bar)return;
    const capped=state.transcriptLimit>=TRANSCRIPT_PAGE_MAX;
    bar.hidden=!(state.hasMore&&state.mode==='chat'&&state.selected);
    button.disabled=!!state.loadingEarlier||capped;
    button.textContent=state.loadingEarlier?'加载中…':(capped?`只保留最近 ${TRANSCRIPT_PAGE_MAX} 条`:'加载更早的消息');
    button.title=capped?`一次最多展开 ${TRANSCRIPT_PAGE_MAX} 条（性能预算：约 12k 节点封顶），再多就卡了`:'把上一次的 300 条再接上来';}
  // The message at the top of the viewport, with its distance from the
  // viewport top. Used to keep the reader's place across a rebuild: a
  // scrollHeight delta cannot do that under content-visibility, where
  // off-screen messages count their 150px intrinsic size and a rebuild
  // forgets every remembered size (the delta can go negative and throw the
  // reader to the top). The anchor's own position is measured live.
  function firstVisibleMessage(){const host=$('transcript'),viewTop=scroll.getBoundingClientRect().top;
    for(const node of host.querySelectorAll('.chat-message')){
      const rect=node.getBoundingClientRect();
      if(rect.bottom>viewTop+1)return {id:node.dataset.id||'',top:rect.top-viewTop};}
    return null;}
  function restoreAnchor(anchor){if(!anchor||!anchor.id)return;
    const node=$('transcript').querySelector(`[data-id="${CSS.escape(anchor.id)}"]`);
    if(!node)return;
    const viewTop=scroll.getBoundingClientRect().top;
    scroll.scrollTop+=node.getBoundingClientRect().top-viewTop-anchor.top;}
  async function loadEarlier(){const id=state.selected;
    if(!id||!state.hasMore||state.loadingEarlier||state.transcriptLimit>=TRANSCRIPT_PAGE_MAX)return;
    state.loadingEarlier=true;syncEarlierBar();
    const previous=state.transcriptMessages.length,anchor=firstVisibleMessage();
    const limit=Math.min(TRANSCRIPT_PAGE_MAX,state.transcriptLimit*2);
    try{const d=await api(`/api/panes/${encodeURIComponent(id)}/transcript?limit=${limit}`);
      if(state.selected!==id||state.mode!=='chat')return;
      const messages=d.messages||[];
      state.transcriptLimit=limit;
      state.hasMore=!!d.has_more&&messages.length>previous;
      rememberTranscript(id,d);state.transcriptSig=d.signature||transcriptSignature(d);state.transcriptMtime=d.mtime;
      renderTranscriptNow(messages);
      requestAnimationFrame(()=>{restoreAnchor(anchor);state.stick=false;});
    }catch(error){if(state.selected===id)showError(error.message);}
    finally{if(state.selected===id){state.loadingEarlier=false;syncEarlierBar();}}}
  function cachedTranscript(id){
    const memory=state.transcripts.get(id);
    if(memory)return memory;
    if(!window.TmuxPaneCache)return null;
    const disk=TmuxPaneCache.readTranscript(id,7*24*3600*1000);
    if(!disk)return null;
    // 从磁盘读出来的也放进内存工作集，后面 refresh 会就地覆盖
    state.transcripts.set(id,{...disk,at:0,mode:'transcript'});
    return state.transcripts.get(id);
  }
  function rememberTranscript(id,d){if(!id||!d||!Array.isArray(d.messages))return;const previous=cachedTranscript(id)||{};
    const panePid=state.panes.find(p=>p.id===id)?.pane_pid||'';
    state.transcripts.set(id,{...previous,messages:d.messages,provider:d.provider||previous.provider||'',
      mode:d.mode||'transcript',panePid,mtime:d.mtime,sig:d.signature||transcriptSignature(d),at:Date.now()});
    if(window.TmuxPaneCache)TmuxPaneCache.saveTranscript(id,{messages:d.messages,panePid,provider:d.provider||'',mtime:d.mtime,sig:d.signature||transcriptSignature(d)});
    // keep the working set small: eight panes is more than a human switches between
    if(state.transcripts.size>8){const stale=[...state.transcripts.entries()].filter(([key])=>key!==id&&key!==state.selected)
      .sort((a,b)=>a[1].at-b[1].at);for(const [key] of stale.slice(0,state.transcripts.size-8))state.transcripts.delete(key);}}
  function savePaneUi(id){if(!id)return;const entry=cachedTranscript(id);if(entry){entry.scrollTop=scroll.scrollTop;entry.stick=state.stick;}state.drafts.set(id,input.value);}
  function restorePaneUi(id){const entry=cachedTranscript(id);if(!entry)return;
    requestAnimationFrame(()=>{const top=entry.scrollTop==null?scroll.scrollHeight:entry.scrollTop;
      scroll.scrollTop=top;state.stick=entry.stick!==false;});}
  function paintCachedTranscript(id){const entry=cachedTranscript(id);if(!entry||!entry.messages||!entry.messages.length)return false;
    term.textContent='';$('transcript').classList.add('active');$('message-shell').hidden=true;
    clientTrace(null,`cache:${entry.messages.length}`,id);
    state.transcriptMtime=entry.mtime;state.transcriptSig=entry.sig;state.transcriptProvider=entry.provider||'';renderTranscriptNow(entry.messages);
    state.needsFullTranscript=true;
    $('status-dot').className='status-dot live';$('status-text').textContent=`${entry.provider||'缓存'} · 更新中…`;
    restorePaneUi(id);return true;}
  // ---- 下载线路：浏览器 → 本机这一跳（TS / 局域网 / CF）；点子节点 pane 时也切换它的线路
  const ROUTE_LABELS = { ts: 'TS', lan: '局域网', cf: 'CF' };
  const ROUTE_ORDER = ['ts', 'lan', 'cf'];
  const ROUTE_KIND = { ts: 'tailnet', lan: 'lan', cf: 'public' };

  function paintRoute(value) {
    const button = $('route-switch');
    if (!button) return;
    const route = ROUTE_LABELS[value] ? value : 'ts';
    button.textContent = ROUTE_LABELS[route];
    button.dataset.route = route;
    button.title = `下载线路：${ROUTE_LABELS[route]}（点一下换下一条：TS → 局域网 → CF）`;
  }

  function currentRoute() {
    try {
      if (window.TmuxTransfer && TmuxTransfer.routePref) return TmuxTransfer.routePref();
    } catch { /* 没加载就用默认 */ }
    return 'ts';
  }

  async function cycleRoute() {
    const next = ROUTE_ORDER[(ROUTE_ORDER.indexOf(currentRoute()) + 1) % ROUTE_ORDER.length];
    try {
      if (window.TmuxTransfer && TmuxTransfer.setRoutePref) TmuxTransfer.setRoutePref(next);
      if (window.TmuxTransfer && TmuxTransfer.forget) TmuxTransfer.forget();  // 换线路就别用旧的缓存源站
    } catch { /* 记不住就只在这一次生效 */ }
    paintRoute(next);
    const pane = String(state.selected || '');
    if (!pane.includes('~')) { toast(`下载线路：${ROUTE_LABELS[next]}`); return; }
    const hostId = pane.split('~')[0];
    try {
      const saved = await api(`/api/fed/nodes/${encodeURIComponent(hostId)}/route`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: ROUTE_KIND[next] }),
      });
      toast(`下载线路：${ROUTE_LABELS[next]} · 子节点现在走 ${saved.base_url}`);
    } catch (error) {
      const missing = /没有/.test(String(error.message || ''));
      toast(missing ? `这个节点没有${ROUTE_LABELS[next]}地址` : `切换失败：${error.message}`);
    }
  }

  function closeChatReply(){
    const panel=$('chat-reply-panel');
    panel.querySelector('iframe')?.remove();
    panel.hidden=true;
  }

  function replyFrame(pane){
    const frame=document.createElement('iframe');
    frame.title='实时作答';
    frame.src='/term?embed=1&reply=1&pane='+encodeURIComponent(pane);
    return frame;
  }

  $('chat-reply-panel').addEventListener('open-reply',event=>{
    if(!state.selected || state.mode!=='chat')return;
    closeChatReply();
    const panel=$('chat-reply-panel');
    const frame=replyFrame(state.selected);
    $('chat-reply-title').textContent='实时作答 · '+(event.detail?.label || $('selected-title').textContent);
    panel.hidden=false;
    panel.appendChild(frame);
    $('chat-reply-close').focus();
  });
  $('chat-reply-close').onclick=()=>{
    closeChatReply();
    const inspect=$('inspect-interaction');
    (inspect.hidden ? ($('live-tail').hidden ? $('chat-mode') : $('live-tail-toggle')) : inspect).focus();
  };
  function selectPane(id,{remember=true}={}){
    if(remember){state.wantedPane=null;rememberPane(id);}
    if(state.selected!==id){closeInputComplete();window.TmuxChatSearch?.close();}closeChatReply();if(state.selected!==id)collapsedTailPane=null;if(state.selected&&state.selected!==id){savePaneUi(state.selected);clearAttachments();}syncAttachments();
    setTimeout(syncAttachments,0);if(window.TmuxRecents){TmuxRecents.touch(id);TmuxRecents.markActive(id);}closeConnections();state.questionNotice=null;state.questionPane=null;state.selected=id;state.transcriptProvider='';syncNativeForm();if(window.TmuxPrompt)TmuxPrompt.refresh();state.transcriptMtime=null;state.transcriptSig=null;state.transcriptMessages=[];state.transcriptLimit=TRANSCRIPT_PAGE;state.hasMore=false;state.loadingEarlier=false;state.waiting=false;state.waitingSince=null;$('transcript').classList.remove('active');syncProgress();if(state.filesPane&&state.filesPane!==id){state.filesPane=null;state.filesData=null;state.filesPath=null;$('file-list').innerHTML='';showPath('');state.filesPicked.clear();syncFileSelection();$('file-host').textContent='';$('file-host').hidden=true;setFileStatus('');}renderList();term.textContent='';$('transcript-messages').innerHTML='';state.renderedMessages=new Map();$('message-shell').hidden=!id;$('empty').style.display=id?'none':'grid';input.disabled=!id;send.disabled=!id;if($('inspect-interaction'))$('inspect-interaction').disabled=!id;input.value=id?(state.drafts.get(id)||''):'';$('status-dot').className='status-dot';$('status-text').textContent=id?'连接中…':'未连接';if(!id){$('selected-title').textContent='选择一个会话';document.title='Tmux Chat';$('selected-number').hidden=true;$('selected-sub').textContent='从左侧选择 tmux pane';syncFavButton();const ab=$('agents-button');if(ab)ab.disabled=true;if(window.TmuxAgents)TmuxAgents.close();
      const sb=$('chat-search-button');if(sb)sb.disabled=true;if(window.TmuxChatSearch)TmuxChatSearch.close();return;}const p=state.panes.find(x=>x.id===id);const num=$('selected-number');if(num){num.hidden=!p.number;num.textContent=p.number?paneLabel(p):'';num.title=p.number?`窗口编号 ${paneLabel(p)}（relay 里可以直接写这个）`:'';}const customName=(window.TmuxRecents&&TmuxRecents.nameFor)?TmuxRecents.nameFor(id):'';const shownTitle=customName||p.title||p.target;$('selected-title').textContent=shownTitle;document.title=p.number?`${paneLabel(p)} · ${shownTitle} · Tmux Chat`:'Tmux Chat';paintPaneHeader(p);$('agent-name').textContent=p.command;$('agent-mark').textContent=(p.command||'T')[0].toUpperCase();$('share-button').disabled=!(p.capabilities&&p.capabilities.share);$('agents-button').disabled=false;if(window.TmuxAgents)TmuxAgents.refresh();
    const searchBtn=$('chat-search-button');if(searchBtn)searchBtn.disabled=false;syncLiveButton();syncFavButton();
    queueState.items=[];renderQueueStrip();
    clientTrace(null,'sel:cleared',id);
    if(state.mode==='terminal'){connectTerminal(id);}
    else if(paintCachedTranscript(id)){clientTrace(null,'sel:cached',id);refreshTranscript(id);}  // instant paint, then revalidate
    else {connectTranscript(id,true);}
    clientTrace(null,'sel:connected',id);
    resetPaneModels(id);
    clientTrace(null,'sel:models',id);
    loadQueue().then(()=>scheduleQueuePoll(4000));
    if(state.mode==='chat'&&canAutoFocusComposer())input.focus();
    const memory=filesMemoryOn();
    const remembered=memory?readFilesView(id):null;
    if(memory){
      // 开启记忆后，没见过的 pane 就是"上次没开文件面板"，不要从上一个 pane 继承
      if(remembered&&remembered.open){state.filesMode=true;openFiles(remembered.path||null);}
      else {state.filesMode=false;syncFilesPanel();}
    }else if(state.filesMode){openFiles(null);}
    // 选完 pane 要把抽屉「连同那层灰底」一起关掉：只 remove('open') 会留下
    // .drawer-backdrop.show，手机上就是一层灰色遮罩，得再点一下才消失
    closeSidebar();}
  function transcriptSignature(d){const messages=d.messages||[];return `${d.mtime}|${messages.length}|${messages.length?messages[messages.length-1].id:''}`;}
  function mergeIncrementalMessages(existing,incoming,sinceId){
    // 增量只在锚点仍是本地列表尾时才能接上；对不上（切 pane/加载更早消息/会话轮换）
    // 就返回 null，调用方走全量刷新，绝不把两段不连续的消息拼在一起
    if(!Array.isArray(existing)||!existing.length||!Array.isArray(incoming))return null;
    if(String(existing[existing.length-1].id)!==String(sinceId))return null;
    const seen=new Set(existing.map(message=>String(message.id)));
    return existing.concat(incoming.filter(message=>!seen.has(String(message.id)))).slice(-TRANSCRIPT_PAGE_MAX);}
  function atBottom(){return scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<90;}
  function htmlEscape(value){return value.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function stripAnsi(value){return value.replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))/g,'');}
  function cleanDisplayAnsi(value){return value.replace(/\x1b\[([0-9;]*)m/g,(all,raw)=>{const values=(raw||'0').split(';').map(Number),kept=[];for(let i=0;i<values.length;i++){const n=values[i];if(n===7||n===27||(n>=40&&n<=49)||(n>=100&&n<=107))continue;if(n===48){if(values[i+1]===5)i+=2;else if(values[i+1]===2)i+=4;continue;}kept.push(n);}return kept.length?`\x1b[${kept.join(';')}m`:'';});}
  function terminalWidth(ch){const cp=ch.codePointAt(0);if(/\p{Mark}/u.test(ch)||cp===0x200d||cp===0xfe0f)return 0;return cp>=0x1100&&(cp<=0x115f||cp===0x2329||cp===0x232a||(cp>=0x2e80&&cp<=0xa4cf&&cp!==0x303f)||(cp>=0xac00&&cp<=0xd7a3)||(cp>=0xf900&&cp<=0xfaff)||(cp>=0xfe10&&cp<=0xfe19)||(cp>=0xfe30&&cp<=0xfe6f)||(cp>=0xff00&&cp<=0xff60)||(cp>=0xffe0&&cp<=0xffe6)||(cp>=0x1f300&&cp<=0x1faff)||(cp>=0x20000&&cp<=0x3fffd))?2:1;}
  function isTableLine(line){const plain=stripAnsi(line);return /[┌┬┐├┼┤└┴┘│─]/u.test(plain);}
  function renderTableLine(line){const plain=stripAnsi(line),cells=[];for(const ch of plain){const width=terminalWidth(ch),kind=width===2?' wide':width===0?' zero':'';cells.push(`<span class="terminal-cell${kind}">${ch===' '? '&nbsp;':htmlEscape(ch)}</span>`);}return `<span class="terminal-table-line">${cells.join('')}</span>`;}
  function renderCapture(content){return cleanDisplayAnsi(content).split('\n').map(line=>isTableLine(line)?renderTableLine(line):ansi.ansi_to_html(line)).join('\n');}
  // Escape raw HTML through the renderer: pre-escaping the source double-escaped every
  // code block ("-->" became "--&gt;"), which also broke mermaid sources.
  const mdEscape=value=>String(value==null?'':value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  marked.use({renderer:{html(token){return mdEscape(token&&(token.text!=null?token.text:token.raw));}}});
  // 渲染器崩溃排查用：`?safe=1` 关掉所有后处理（路径链接/视频卡片/交互卡片/mermaid），
  // `?plain=1` 连 markdown 都不解析。Chrome 的 STATUS_BREAKPOINT 是整页崩溃，控制台里
  // 什么都看不到，只能靠这两个开关把范围切小。
  const URL_FLAGS=new URLSearchParams(location.search);
  // 性能预算（?perf=1）：左上角实时指标，超标立即变红并写进服务日志
  const PERF_MODE=new URLSearchParams(location.search).get('perf')==='1'
    || (()=>{try{return localStorage.getItem('tmux-perf')==='1'}catch{return false}})();
  if(PERF_MODE&&window.TmuxPerf)window.TmuxPerf.start();
  const SAFE_MODE=URL_FLAGS.get('safe')==='1';
  const PLAIN_MODE=URL_FLAGS.get('plain')==='1';
  const NOLINK_MODE=URL_FLAGS.get('nolink')==='1';      // 只关路径链接
  const NOVIDEO_MODE=URL_FLAGS.get('novideo')==='1';    // 只关视频卡片
  window.__tmuxSafeMode=SAFE_MODE;
  window.addEventListener('error',event=>{try{clientTrace(null,`jserr:${(event.message||'').slice(0,80)}`);}catch(_e){}});
  window.addEventListener('unhandledrejection',event=>{try{clientTrace(null,`reject:${String(event.reason).slice(0,80)}`);}catch(_e){}});
  // 崩溃取线索：每渲染一批就报一条"画到哪了"，服务端日志里能看到最后一次是哪条消息。
  // 用 sendBeacon 不占请求队列；页面对同一个 pane 的同一批只报一次。
  let traceAt=0, traceKey='';
  // 滚动时才崩 → 光知道"渲染到哪"不够，还得知道"当时屏幕上是谁"。
  let visObserver=null, visPending=null, visTimer=null;
  function watchVisibility(host){
    if(!('IntersectionObserver' in window))return;
    if(!visObserver){
      visObserver=new IntersectionObserver(entries=>{
        const seen=new Set(visPending||[]);
        entries.forEach(entry=>{
          const id=entry.target.dataset.id;
          if(!id)return;
          if(entry.isIntersecting)seen.add(id);else seen.delete(id);
        });
        visPending=[...seen];
        clearTimeout(visTimer);
        visTimer=setTimeout(()=>{clientTrace([],'visible:'+(visPending||[]).join(','));},400);
      },{threshold:0.35});
    }
    host.querySelectorAll('.chat-message[data-id]').forEach(node=>{
      if(node.dataset.visWatched==='1')return;
      node.dataset.visWatched='1';
      visObserver.observe(node);
    });
  }
  function clientTrace(messages, note, paneOverride){
    try{
      if(!state.selected&&!paneOverride)return;
      const last=(messages&&messages.length)?messages[messages.length-1]:null;
      const pane=paneOverride||state.selected;
      const key=`${pane}|${last?last.id:''}|${messages?messages.length:0}|${note||''}`;
      if(key===traceKey&&Date.now()-traceAt<4000)return;
      traceKey=key;traceAt=Date.now();
      const body=JSON.stringify({pane,last_id:last?String(last.id):'',rendered:messages?messages.length:0,note:note||''});
      if(navigator.sendBeacon)navigator.sendBeacon('/api/client-trace',new Blob([body],{type:'application/json'}));
      else fetch('/api/client-trace',{method:'POST',headers:{'Content-Type':'application/json'},body,keepalive:true});
    }catch{}
  }
  function markdown(value){const raw=String(value==null?'':value);
    if(PLAIN_MODE||window.__tmuxPlainMode)return `<pre class="plain-message">${mdEscape(raw)}</pre>`;
    // 中文加粗的 CommonMark 围栏问题：**任务：**它 → 不渲染，见 assets/markdown-cjk.js
    const fixed=window.TmuxMarkdownCjk?window.TmuxMarkdownCjk.fix(raw):raw;
    const html=marked.parse(fixed,{gfm:true,breaks:false,async:false});
    return window.TmuxMarkdownCjk?window.TmuxMarkdownCjk.strip(html):html;}
  // Compare messages the way a human would: the agent may re-wrap, trim or add blank lines
  function messageTextKey(text){return String(text==null?'':text).replace(/\r\n/g,'\n').replace(/[ \t]+/g,' ').replace(/\s+/g,' ').trim();}
  function reconcilePending(messages){
    const waiting=[...state.pendingMessages].filter(([,pending])=>pending.pane===state.selected&&pending.status!=='failed')
      .sort((a,b)=>(a[1].createdAt||0)-(b[1].createdAt||0));
    if(!waiting.length)return;
    // only messages that were not part of the transcript when the user hit send
    const baseline=waiting[0][1].baselineIds;
    const fresh=messages.filter(message=>message.role==='user'&&!baseline.has(String(message.id)));
    if(!fresh.length)return;
    const used=new Set();
    const claimed=new Map();
    // exact text first: the transcript usually echoes exactly what was typed
    for(const [localId,pending] of waiting){
      const key=messageTextKey(pending.text);
      const match=fresh.find(message=>!used.has(String(message.id))&&
        (messageTextKey(message.text)===key||messageTextKey(message.text).includes(key)));
      if(match){used.add(String(match.id));claimed.set(localId,true);}}
    // then by order: a rewritten/expanded echo still belongs to the same send
    for(const [localId] of waiting){
      if(claimed.has(localId))continue;
      const next=fresh.find(message=>!used.has(String(message.id)));
      if(!next)break;
      used.add(String(next.id));claimed.set(localId,true);}
    for(const localId of claimed.keys())state.pendingMessages.delete(localId);}
  // A sent bubble belongs right after the transcript as it looked when the user hit send,
  // not after whatever the agent wrote in the meantime.
  function pendingAnchorIndex(messages,pending){const baseline=pending.baselineIds||new Set();
    for(let index=messages.length-1;index>=0;index--){if(baseline.has(String(messages[index].id)))return index+1;}
    return 0;}
  // 联邦 pane 的编号带节点前缀（ddr#12），本机仍是 #12；两边各自的 #N 会撞号
  function paneLabel(pane){if(!pane||!pane.number)return '—';return (pane.prefix?pane.prefix+'#':'#')+pane.number;}
  function userBubbleHtml(message){
    // 附件那一行只有一个文件名，看不出是什么：交给 upload-cards 换成预览图
    const pane=message.pane||state.selected;
    return window.TmuxUploads?TmuxUploads.bubbleHtml(message.text,pane):htmlEscape(message.text);
  }
  function deliveryHtml(pending,localId){if(pending.status==='sending')return `<span class="message-delivery"><span class="pending-wave"><i></i><i></i><i></i></span>发送中</span>`;if(pending.status==='sent')return `<span class="message-delivery sent">已发送 ✓</span>`;if(pending.status==='retrying')return `<span class="message-delivery"><span class="pending-wave"><i></i><i></i><i></i></span>正在重试提交…</span>`;return `<span class="message-delivery failed">发送失败<button class="retry-send" type="button" data-resend="${esc(localId||'')}" title="重新发送这条消息">重发</button></span>`;}
  function msgResendButton(message){
    if(!message||message.role!=='user')return '';
    return `<button class="msg-copy" type="button" data-msg-resend="${esc(message.id)}" title="把这条消息填回输入框，可修改后再发送"><svg class="ico"><use href="#i-refresh"/></svg><span class="btn-label">再次发送</span></button>`;
  }
  const clockTime=ts=>{
    if(ts===null||ts===undefined||ts==='')return '';
    const numeric=typeof ts==='number'||/^\d+(\.\d+)?$/.test(String(ts).trim());
    const d=numeric?new Date(Number(ts)>1e12?Number(ts):Number(ts)*1000):new Date(ts);
    if(Number.isNaN(d.getTime()))return '';
    const pad=value=>String(value).padStart(2,'0'),now=new Date();
    return `${d.toDateString()===now.toDateString()?'':`${pad(d.getMonth()+1)}-${pad(d.getDate())} `}${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  function transcriptBubble(message, plain=false){
    if(message.role==='interaction'){
      return '<article class="chat-message assistant" data-id="'+esc(message.id)+'"><div class="assistant-body"><strong>提问记录</strong><pre class="interaction-text">'+htmlEscape(message.text)+'</pre></div></article>';
    }
    const clock=clockTime(message.timestamp);
    const copy=`<div class="msg-actions">${clock?`<span class="msg-time">${esc(clock)}</span>`:''}${msgResendButton(message)}<button class="msg-copy" type="button" data-msg-copy="${esc(message.id)}" title="复制整条消息（含代码/表格源码）"><svg class="ico"><use href="#i-copy"/></svg><span class="btn-label">复制全部</span></button></div>`;
    const text=plain?`<pre class="plain-message">${htmlEscape(String(message.text||''))}</pre>`:null;
    return message.role==='user'
      ? `<article class="chat-message user" data-id="${esc(message.id)}"><div class="chat-user-wrap"><div class="chat-bubble">${text??userBubbleHtml(message)}</div>${copy}</div></article>`
      : `<article class="chat-message assistant" data-id="${esc(message.id)}"><div class="assistant-body">${text??markdown(message.text)}${copy}</div></article>`;
  }
  // Borrowed from Open WebUI (src/lib/components/chat/Messages/Markdown.svelte:121):
  // while streaming, marked parses at most once per animation frame — every
  // update that lands inside the same frame only replaces the pending text —
  // and when done the final text parses immediately. Upstream cancels the
  // pending frame in onDestroy; here a pane switch / mode change does the
  // same (closeConnections), and the frame callback drops a render whose
  // pane is no longer selected, so a stale frame can never paint over a
  // new pane. The other half of A2 — streamLargeDeltasAsRandomChunks in
  // streaming/index.ts, re-chunking large deltas into random 1–3 char
  // pieces — is a typewriter effect for providers that send large chunks
  // at once; TmuxWeb's transcript arrives whole, so that half is
  // deliberately not taken.
  let transcriptRenderFrame=null, transcriptRenderPending=null;
  function cancelScheduledTranscriptRender(){if(transcriptRenderFrame!=null){cancelAnimationFrame(transcriptRenderFrame);transcriptRenderFrame=null;}transcriptRenderPending=null;}
  function renderTranscriptNow(messages){cancelScheduledTranscriptRender();renderTranscript(messages);}
  function scheduleTranscriptRender(paneId,messages,afterRender){
    transcriptRenderPending={paneId,messages,afterRender};
    if(transcriptRenderFrame!=null)return;
    transcriptRenderFrame=requestAnimationFrame(()=>{
      transcriptRenderFrame=null;
      const pending=transcriptRenderPending;transcriptRenderPending=null;
      if(!pending||state.selected!==pending.paneId||state.mode!=='chat')return;
      renderTranscript(pending.messages);
      if(pending.afterRender)pending.afterRender();});}
  // Streaming poll path: coalesce into one render per frame; the final
  // update (agent no longer waiting) renders immediately, like upstream's
  // `done` branch. The scroll-to-bottom runs inside the same frame, right
  // after the DOM write — never ahead of it.
  function applyTranscriptUpdate(paneId,messages,scrollToBottom,done){
    const after=scrollToBottom?()=>{scroll.scrollTop=scroll.scrollHeight;}:null;
    if(done){renderTranscriptNow(messages);if(after)after();return;}
    scheduleTranscriptRender(paneId,messages,after);}
  // A streaming agent appends one message at a time. Rebuilding every bubble (and
  // re-parsing every markdown block) for each of them is what made a long chat
  // feel heavy, so an append-only update only renders the new tail.
  function renderTranscript(messages=state.transcriptMessages){hideLoadCard();
    messages=messages.slice(-TRANSCRIPT_PAGE_MAX);
    state.transcriptMessages=messages;
    state.messageText=new Map(messages.map(message=>[String(message.id),String(message.text||'')]));
    reconcilePending(messages);
    const host=$('transcript-messages'),before=state.renderedMessages||new Map(),next=new Map();
    const pending=new Map();
    const local=[...state.pendingMessages].filter(([,message])=>message.pane===state.selected)
      .sort((a,b)=>(a[1].createdAt||0)-(b[1].createdAt||0));
    const pendingTail=local.slice(-TRANSCRIPT_PAGE_MAX);
    const formal=messages.slice(Math.max(0,messages.length+pendingTail.length-TRANSCRIPT_PAGE_MAX));
    for(const [id,message] of pendingTail){
      const index=pendingAnchorIndex(formal,message),slot=pending.get(index)||[];
      slot.push({id,message});pending.set(index,slot);
    }
    const ordered=[];
    const holder=document.createElement('div');
    let budget=9500-(formal.length+pendingTail.length)*14;
    let changed=false;
    function append(id,revision,makeHtml,fallback){
      let item=before.get(id);
      const same=item&&(Array.isArray(revision)
        ? revision.every((value,index)=>value===item.revision[index]):item.revision===revision);
      if(!same||item.cost>budget+14){
        holder.innerHTML=makeHtml();
        enhanceBlocks(holder);rewriteLocalLinks(holder);
        if(fallback&&holder.querySelectorAll('*').length>budget+14)holder.innerHTML=fallback();
        item={revision,node:holder.firstElementChild,cost:holder.querySelectorAll('*').length};
        changed=true;
      }
      budget=Math.max(0,budget+14-item.cost);
      next.set(id,item);ordered.push(item.node);
    }
    for(let index=0;index<=formal.length;index++){
      for(const {id,message} of pending.get(index)||[]){
        const bubble=plain=>`<article class="chat-message user pending" data-id="${esc(id)}"><div class="chat-user-wrap"><div class="chat-bubble">${plain?htmlEscape(message.text):userBubbleHtml(message)}</div>${deliveryHtml(message,id)}</div></article>`;
        append(id,message.status,()=>bubble(false),()=>bubble(true));
      }
      if(index===formal.length)break;
      const message=formal[index],id=String(message.id);
      const revision=[message.role,message.timestamp,message.text];
      append(id,revision,()=>transcriptBubble(message),()=>transcriptBubble(message,true));
    }
    for(const [id,item] of before){
      if(next.get(id)===item)continue;
      visObserver?.unobserve(item.node);
      item.node.remove();
      changed=true;
    }
    let position=host.firstElementChild;
    for(const node of ordered){
      if(node===position)position=position.nextElementSibling;
      else {host.insertBefore(node,position);changed=true;}
    }
    for(const node of [...host.children])if(!next.has(node.dataset.id))node.remove();
    state.renderedMessages=next;
    host.classList.add('active');$('message-shell').hidden=true;
    if(changed){enhancePathsLazy(host,true);if(PERF_MODE)watchVisibility(host);clientTrace(messages,'update');}
  }
  const CODE_LANG_RE=/language-([\w+#-]+)/;
  async function copyText(text,button){const value=String(text==null?'':text);let ok=false;
    try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(value);ok=true;}}catch{}
    if(!ok){const area=document.createElement('textarea');area.value=value;area.setAttribute('readonly','');
      area.style.cssText='position:fixed;top:-1000px;left:-1000px;opacity:0';document.body.appendChild(area);
      area.select();try{ok=document.execCommand('copy');}catch{ok=false;}area.remove();}
    if(button){const label=button.querySelector('.btn-label')||button;const original=label.textContent;
      button.classList.toggle('copied',ok);
      label.textContent=ok?'已复制':'复制失败';
      setTimeout(()=>{label.textContent=original;button.classList.remove('copied');},1400);}
    return ok;}
  function tableMarkdown(table){const rows=[...table.querySelectorAll('tr')];if(!rows.length)return '';
    const line=row=>'| '+[...row.children].map(cell=>String(cell.textContent).trim().replace(/\|/g,'\\|')).join(' | ')+' |';
    const head=line(rows[0]),width=rows[0].children.length,divider='| '+Array(width).fill('---').join(' | ')+' |';
    return [head,divider,...rows.slice(1).map(line)].join('\n');}
  function tableTsv(table){return [...table.querySelectorAll('tr')].map(row=>[...row.children].map(cell=>String(cell.textContent).trim()).join('\t')).join('\n');}
  function blockShell(kind,kindLabel){const wrap=document.createElement('div');wrap.className='block-wrap';wrap.dataset.kind=kind;
    wrap.innerHTML=`<div class="block-bar"><span class="block-kind">${kindLabel}</span>`
      +`<button class="block-btn" type="button" data-block-copy title="${kind==='table'?'复制为 TSV':kind==='mermaid'?'复制 mermaid 源码':'复制代码'}"><svg class="ico"><use href="#i-copy"/></svg><span class="btn-label">复制</span></button>`
      +(kind==='table'||kind==='mermaid'?`<button class="block-btn" type="button" data-block-source title="切换源码/渲染">源码</button>`:'')
      +'</div>';return wrap;}
  let mermaidLoading=null;
  function loadMermaid(){if(window.mermaid)return Promise.resolve(window.mermaid);
    if(mermaidLoading)return mermaidLoading;
    mermaidLoading=new Promise((resolve,reject)=>{const script=document.createElement('script');
      script.src='/assets/mermaid/mermaid.min.js';
      script.onload=()=>{try{window.mermaid.initialize({startOnLoad:false,theme:'neutral',securityLevel:'strict',
        flowchart:{htmlLabels:false},fontFamily:'ui-sans-serif,system-ui'});resolve(window.mermaid);}catch(error){reject(error);}};
      script.onerror=()=>reject(new Error('无法加载 mermaid'));document.head.appendChild(script);});
    return mermaidLoading;}
  function enhanceBlocks(root){root.querySelectorAll('article.assistant .assistant-body').forEach(body=>{
    body.querySelectorAll('pre').forEach(pre=>{if(pre.closest('.block-wrap'))return;
      const code=pre.querySelector('code')||pre,source=code.textContent||'',
        lang=(code.className.match(CODE_LANG_RE)||[])[1]||'';
      if(lang==='mermaid'){const wrap=blockShell('mermaid','mermaid');wrap.dataset.source=source;
        const rendered=document.createElement('div');rendered.className='mermaid-target block-rendered';
        rendered.innerHTML='<span class="block-kind">渲染中…</span>';
        const sourceView=document.createElement('pre');sourceView.className='block-source';sourceView.hidden=true;sourceView.textContent=source;
        wrap.append(rendered,sourceView);pre.replaceWith(wrap);
        loadMermaid().then(mermaid=>mermaid.render('m'+Math.random().toString(36).slice(2),source))
          .then(result=>{
            const markup=result&&result.svg?result.svg:String(result||'');
            const elements=(markup.match(/<[a-z]/gi)||[]).length;
            if(!rendered.isConnected)return;
            if(elements+document.getElementsByTagName('*').length>12000){
              sourceView.hidden=false;wrap.dataset.sourceShown='1';rendered.hidden=true;return;
            }
            rendered.innerHTML=markup;
          })
          .catch(error=>{rendered.innerHTML=`<p class="block-error">mermaid 渲染失败：${htmlEscape(String(error.message||error))}</p>`;
            sourceView.hidden=false;wrap.dataset.sourceShown='1';});
        return;}
      const wrap=blockShell('code',lang||'code');wrap.dataset.source=source;
      pre.replaceWith(wrap);wrap.appendChild(pre);});
    body.querySelectorAll('table').forEach(table=>{if(table.closest('.block-wrap'))return;
      const wrap=blockShell('table','table');wrap.dataset.source=tableMarkdown(table);wrap.dataset.tsv=tableTsv(table);
      const sourceView=document.createElement('pre');sourceView.className='block-source';sourceView.hidden=true;sourceView.textContent=wrap.dataset.source;
      const holder=document.createElement('div');holder.className='table-holder block-rendered';
      table.replaceWith(wrap);wrap.append(holder,sourceView);holder.appendChild(table);});});}
  function toggleBlockSource(wrap,button){const source=wrap.querySelector('.block-source');const target=wrap.querySelector('.block-rendered');
    if(!source||!target)return;const showSource=source.hidden;source.hidden=!showSource;target.hidden=showSource;
    button.textContent=showSource?'渲染':'源码';button.setAttribute('aria-pressed',String(showSource));}
  function addOptimisticMessage(text){const localId=`pending-${Date.now()}-${Math.random().toString(36).slice(2)}`,baselineIds=new Set(state.transcriptMessages.map(message=>String(message.id)));state.pendingMessages.set(localId,{pane:state.selected,text,submit:true,status:'sending',baselineIds,createdAt:Date.now()});
    state.waiting=true;state.waitingSince=Date.now()/1000;syncProgress();renderTranscriptNow();requestAnimationFrame(()=>{scroll.scrollTop=scroll.scrollHeight;state.stick=true;});return localId;}
  function setOptimisticStatus(localId,status){const pending=state.pendingMessages.get(localId);if(!pending)return;pending.status=status;renderTranscriptNow();requestAnimationFrame(()=>scroll.scrollTop=scroll.scrollHeight);}
  // 轮询间隔：跑任务时靠 1.5s 的 tail 给实时感，transcript 2s 看一次结果就够了；
  // 空闲越久越省：30s 内 1.5s，之后 3s，5 分钟后 5s，10 分钟后 10s。
  // 高延迟链路上每次轮询都是一次往返，空闲时拉长间隔是最便宜的省法。
  function transcriptPollDelay(){
    if(state.waiting)return 2000;
    const idleFor=state.lastTranscriptChange?Date.now()-state.lastTranscriptChange:1e9;
    return idleFor<30000?1500:idleFor<300000?3000:idleFor<600000?5000:10000;
  }
  async function refreshTranscript(id,{force=false,refresh=false}={}){if(state.selected!==id||state.mode!=='chat')return;
    const fullFirst=!!state.needsFullTranscript;state.needsFullTranscript=false;
    const limit=state.transcriptLimit;
    // 缓存只画了最后 40 条，全量补齐后内容会变长：读者原本就在最底部，补完也得还在底部
    const follow=atBottom()||fullFirst,first=state.transcriptMtime===null;
    // 稳态轮询才挂长轮询：force/手动刷新走全量立即回；有基线（sig 或 since）才有得等
    const steady=!force&&!refresh,longPoll=steady&&(!!state.transcriptSig||state.transcriptMessages.length>0);
    const sinceId=(!force&&!refresh&&!fullFirst&&state.transcriptMessages.length)?String(state.transcriptMessages[state.transcriptMessages.length-1].id):'';
    let nextDelay=null;
    try{const sig=!fullFirst&&state.transcriptSig?`&sig=${encodeURIComponent(state.transcriptSig)}`:'';
      const since=sinceId?`&since=${encodeURIComponent(sinceId)}`:'';
      const wait=longPoll?'&wait=20':'';
      const d=await api(`/api/panes/${encodeURIComponent(id)}/transcript?limit=${limit}${refresh?'&refresh=1':''}${sig}${since}${wait}`);if(state.selected!==id||state.mode!=='chat'||state.transcriptLimit!==limit)return;
      // 后台重试的状态也走这条轮询：必须在 unchanged 早退之前同步，否则
      // transcript 不动时气泡永远收不到终态。
      syncResendState(d);
      syncQuestionNotice(id,d);
      showError('');state.transcriptFailures=0;$('status-dot').className='status-dot live';
      // 服务端真的挂住过（long_poll）才立即再挂；远端 pane 和旧服务端没这个标记，
      // 沿用原来的退避间隔，不会被打成紧循环
      nextDelay=d.long_poll?0:transcriptPollDelay();
      if(d.unchanged){state.waiting=!!d.waiting;state.waitingSince=d.waiting_since||null;syncProgress();
        return;}
      const signature=d.signature||transcriptSignature(d),changed=force||signature!==state.transcriptSig;
      state.hasMore=!!d.has_more;syncEarlierBar();
      if(d.model){modelState.current=d.model;$('model-current').textContent=modelLabel();}
      if(d.provider&&/^(codex|claude|hermes|kimi)$/.test(d.provider))$('model-pick-wrap').hidden=false;
      state.transcriptProvider=d.provider||'';state.waiting=!!d.waiting;state.waitingSince=d.waiting_since||null;syncProgress();
      if(d.incremental){
        const merged=mergeIncrementalMessages(state.transcriptMessages,d.messages,sinceId);
        if(!merged){await refreshTranscript(id,{force:true});return;}
        rememberTranscript(id,{...d,messages:merged});
        if(changed){state.lastTranscriptChange=Date.now();state.transcriptSig=signature;state.transcriptMtime=d.mtime;applyTranscriptUpdate(id,merged,follow||first,!d.waiting);syncProgress();}
        else {syncProgress();}
      }else{
        rememberTranscript(id,d);
        if(changed){state.lastTranscriptChange=Date.now();state.transcriptSig=signature;state.transcriptMtime=d.mtime;applyTranscriptUpdate(id,d.messages,follow||first,!d.waiting);syncProgress();}
        else {syncProgress();}
      }
    }catch(e){state.transcriptFailures=(state.transcriptFailures||0)+1;
      if(state.transcriptFailures>=2&&state.selected===id&&state.mode==='chat')showError(e.message);
      nextDelay=500;}
    finally{
      scheduleTranscriptPoll(id,nextDelay==null?transcriptPollDelay():nextDelay);}}
  const tailNav=$('live-keys-nav'), keyboardToggle=$('live-keyboard-toggle');
  function expandTailKeys(open){
    keyboardToggle.setAttribute('aria-expanded',String(open));
    tailNav.hidden=!open;
    if(open)tailNav.scrollLeft=0;
    else if(tailLoop)tailLoop.keys=tailLoop.keys.filter(entry=>entry.physical);
  }
  keyboardToggle.onclick=()=>{
    if(tailLoop)expandTailKeys(tailNav.hidden);
  };
  const tailText=$('live-tail-text');
  const desktopKeys=matchMedia('(hover:hover) and (pointer:fine)');
  function syncTailKeyboard(){
    tailText.tabIndex=desktopKeys.matches ? 0 : -1;
    tailText.title=desktopKeys.matches?'点击使用物理键盘，Shift+Esc 退出':'';
    if(!desktopKeys.matches && document.activeElement===tailText)tailText.blur();
  }
  desktopKeys.addEventListener('change',syncTailKeyboard);
  syncTailKeyboard();
  const tailNamedKeys={ArrowUp:'Up',ArrowDown:'Down',ArrowLeft:'Left',ArrowRight:'Right',
    Enter:'Enter',Escape:'Escape',Tab:'Tab',' ':'Space',Backspace:'BSpace',Delete:'DC',
    Home:'Home',End:'End',PageUp:'PPage',PageDown:'NPage'};
  tailText.addEventListener('keydown',event=>{
    if(!desktopKeys.matches || document.activeElement!==tailText || document.hidden ||
        !tailLoop || tailNav.disabled || event.isComposing || event.metaKey || event.altKey)return;
    if(event.shiftKey && event.key==='Escape'){
      event.preventDefault();
      $('live-tail-toggle').focus({preventScroll:true});
      return;
    }
    let key=tailNamedKeys[event.key], text='';
    if(event.ctrlKey){
      const letter=event.key.toLowerCase();
      if(event.shiftKey || !['c','d','q','u'].includes(letter) || (letter==='c' && !window.getSelection().isCollapsed))return;
      key='C-'+letter;
    }else if(event.shiftKey && event.key==='Tab')key='BTab';
    else if(!key && /^[A-Za-z0-9]$/.test(event.key))key=event.key;
    else if(!key && [...event.key].length===1)text=event.key;
    if(!key && !text)return;
    event.preventDefault();
    event.stopPropagation();
    if(event.repeat && (tailLoop.keys.length || key==='Enter' || key==='Escape' || event.ctrlKey))return;
    queueTailInput(key,text,true);
  });
  tailText.addEventListener('blur',()=>{
    if(tailLoop)tailLoop.keys=tailLoop.keys.filter(entry=>!entry.physical);
  });
  tailNav.onclick=event=>{
    const button=event.target.closest('button[data-key]');
    if(button && !tailNav.hidden)queueTailInput(button.dataset.key);
  };
  async function queueTailInput(key,text='',physical=false){
    const loop=tailLoop;
    if(!loop || tailNav.disabled || loop.pane!==state.selected)return;
    loop.keys.push({key,text,physical});
    if(loop.sending)return;
    loop.sending=true;
    try{
      while(loop.keys.length && tailLoop===loop && loop.pane===state.selected && !tailNav.disabled){
        const entry=loop.keys.shift();
        await api('/api/panes/'+encodeURIComponent(loop.pane)+(entry.text?'/input':'/key'),{
          method:'POST',headers:{'Content-Type':'application/json'},signal:loop.controller.signal,
          body:JSON.stringify(entry.text?{text:entry.text,submit:false}:{key:entry.key})
        });
      }
    }catch(error){
      if(tailLoop===loop)toast(error.message);
    }finally{
      loop.keys.length=0;
      loop.sending=false;
      if(tailLoop===loop)loop.refresh();
    }
  };
  function cancelTailRead(){
    if(!tailLoop)return;
    window.TmuxPrompt?.observeCodexScreen(tailLoop.pane, null);
    clearTimeout(tailLoop.timer);
    tailLoop.controller.abort();
    tailLoop=null;
    expandTailKeys(false);
    tailNav.disabled=true;
  }
  function showApprovalHint(found){
    const button=$('inspect-interaction');
    const label=found?'查看命令审批':'查看交互';
    if(button.textContent!==label)button.textContent=label;
  }
  function stopLiveTail(){
    showApprovalHint(false);
    cancelTailRead();
    const inspect=$('inspect-interaction'),bar=$('interaction-placement').closest('.cf-bar');
    if(inspect.parentNode!==bar)bar.prepend(inspect);
    $('live-tail').hidden=true;
    $('live-tail-text').textContent='';
  }
  function syncLiveTail(){
    const id=state.selected;
    const box=$('live-tail'),pre=$('live-tail-text'),toggle=$('live-tail-toggle');
    const available=id&&state.mode==='chat'&&!state.livePaused&&$('transcript').classList.contains('active');
    if(!available){stopLiveTail();return;}
    const revision=$('native-form').dataset.open;
    const expanded=collapsedTailPane!==id && (!revision || expandedForm===revision);
    if(box.hidden)box.hidden=false;
    const head=$('live-tail-head'),inspect=$('inspect-interaction');
    if(inspect.parentNode!==head)head.append(inspect);
    if(toggle.getAttribute('aria-expanded')!==String(expanded))toggle.setAttribute('aria-expanded',String(expanded));
    $('live-tail-surface').hidden=!expanded;
    if(!expanded||document.hidden){
      showApprovalHint(false);
      cancelTailRead();
      if(!expanded&&pre.textContent)pre.textContent='';
      return;
    }
    if(tailLoop?.pane===id)return;
    cancelTailRead();
    const loop={pane:id,controller:new AbortController(),timer:null,digest:null,keys:[]};
    tailLoop=loop;
    expandTailKeys(false);
    tailNav.disabled=true;
    pre.textContent='正在读取…';
    loop.refresh=()=>{
      clearTimeout(loop.timer);
      if(loop.reading)loop.again=true;
      else tick();
    };
    async function tick(){
      let delay=1500;
      loop.reading=true;
      try{
        const data=await api('/api/panes/'+encodeURIComponent(id)+'/tail?lines=12&color=1',{signal:loop.controller.signal});
        if(tailLoop!==loop)return;
        tailNav.disabled=false;
        showApprovalHint(data.codex_command_approval===true);
        const notice=data.codex_screen_notice || data.cursor_menu_notice;
        window.TmuxPrompt?.observeCodexScreen(id, data.codex_screen_revision || '', notice);
        const noticeRevision=notice?.revision || '';
        if((loop.noticeRevision || '')!==noticeRevision)window.TmuxNeedsYouInbox?.noticeChanged();
        loop.noticeRevision=noticeRevision;
        if(loop.digest!==data.digest){
          loop.digest=data.digest;
          const styled=data.ansi;
          if(styled && !/[\x00-\x08\x0b-\x1f\x7f-\x9f]/.test(styled.replace(/\x1b\[[0-9;]*m/g,''))){
            pre.innerHTML=tailAnsi.ansi_to_html('\x1b[0m'+styled+'\x1b[0m');
          }else{
            pre.textContent=data.text||'当前屏幕没有文字';
          }
          if(state.stick)scroll.scrollTop=scroll.scrollHeight;
        }else if(!state.waiting&&state.paneState?.get(id)!=='busy'){
          delay=5000;
        }
      }catch(error){
        if(tailLoop!==loop)return;
        showApprovalHint(false);
        tailNav.disabled=true;
        loop.keys.length=0;
        pre.textContent='暂时无法读取终端，稍后重试';
        loop.digest=null;
        delay=5000;
      }
      loop.reading=false;
      if(tailLoop===loop)loop.timer=setTimeout(tick,loop.again?0:delay);
      loop.again=false;
    }
    tick();
  }
  $('native-form').addEventListener('native-form-change',()=>{
    const card=$('native-form'), follow=state.stick;
    expandedForm='';
    syncLiveTail();
    if(card.dataset.open && follow)card.scrollIntoView({block:'start'});
  });
  $('live-tail-toggle').onclick=()=>{
    const open=$('live-tail-toggle').getAttribute('aria-expanded')==='true';
    collapsedTailPane=open?state.selected:null;
    expandedForm=open?'':$('native-form').dataset.open || '';
    syncLiveTail();
  };
  function scheduleTranscriptPoll(id,delay){
    if(!id||state.selected!==id||state.mode!=='chat')return;
    if(!$('transcript').classList.contains('active'))return;
    if(document.hidden)return;                       // resumed by visibilitychange
    clearTimeout(state.transcriptTimer);              // 手动刷新和定时刷新只留一个循环
    state.transcriptTimer=setTimeout(()=>refreshTranscript(id),delay);
  }
  document.addEventListener('visibilitychange',()=>{
    syncNativeForm(true);
    syncLiveTail();
    if(!document.hidden&&state.selected&&state.mode==='chat'&&$('transcript').classList.contains('active')){
      refreshTranscript(state.selected);
    }
  });
  window.addEventListener('focus',()=>{
    if(!document.hidden&&state.selected&&state.mode==='chat'&&$('transcript').classList.contains('active')){
      refreshTranscript(state.selected);
    }
  });
  // Agent CLIs draw a full-screen TUI. Falling back to the raw screen stream for
  // them rebuilds ~550 lines / 7000 nodes twice a second and locks up Chrome, so an
  // empty or unreadable transcript gets a notice plus a retry instead. Shell panes
  // keep the old behaviour (their screen *is* the content).
  const AGENT_COMMAND=/^(claude|codex|kimi|python|hermes|gemini|opencode)/i;
  function isAgentPane(id){const pane=state.panes.find(item=>item.id===id);return !!(pane&&(pane.provider||AGENT_COMMAND.test(String(pane.command||''))));}
  function showTranscriptNotice(id,text){
    hideLoadCard();
    $('transcript').classList.add('active');
    $('transcript-messages').innerHTML='<div class="transcript-empty"><b>暂时没有可解析的对话</b>'
      +`<p>${esc(text)}</p><p class="transcript-empty-actions"><button type="button" id="empty-screen">用终端打开（xterm）</button></p></div>`;
    const button=$('empty-screen');
    if(button)button.onclick=()=>setMode('terminal');
    $('status-dot').className='status-dot';
    $('status-text').textContent='等待消息';
  }
  function retryTranscript(id,delay=4000){
    clearTimeout(state.transcriptTimer);
    state.transcriptTimer=setTimeout(()=>{
      if(state.selected===id&&state.mode==='chat')connectTranscript(id,false);
    },delay);
  }

  async function connectTranscript(id,allowFallback=false){if(!cachedTranscript(id))showLoadCard(id);$('message-shell').hidden=true;$('transcript').classList.add('active');$('status-dot').className='status-dot live';$('status-text').textContent='读取 transcript…';
    try{const d=await apiProgress(`/api/panes/${encodeURIComponent(id)}/transcript?limit=300`,updateLoadCard);if(state.selected!==id||state.mode!=='chat')return;
      syncQuestionNotice(id,d);
      state.waiting=!!d.waiting;state.waitingSince=d.waiting_since||null;
      state.hasMore=!!d.has_more;syncEarlierBar();
      if(!d.messages||!d.messages.length){
        if(isAgentPane(id)){showError('');showTranscriptNotice(id,'这个 agent 会话还没有可读的消息，等它开始工作后会自动刷新。');retryTranscript(id);return;}
        $('transcript').classList.remove('active');if(allowFallback){showError('');connect(id);}else showError('该会话还没有消息');return;}
      showError('');state.transcriptMtime=d.mtime;state.transcriptSig=d.signature||transcriptSignature(d);rememberTranscript(id,d);
      if(d.model){modelState.current=d.model;$('model-current').textContent=modelLabel();}
      if(d.provider&&/^(codex|claude|hermes|kimi)$/.test(d.provider))$('model-pick-wrap').hidden=false;
      state.transcriptProvider=d.provider||'';
      renderTranscriptNow(d.messages);syncProgress();
      const entry=cachedTranscript(id);const top=entry&&entry.scrollTop!=null?entry.scrollTop:scroll.scrollHeight;
      requestAnimationFrame(()=>{scroll.scrollTop=top;state.stick=(entry&&entry.stick===false)?false:true;});
      state.transcriptTimer=setTimeout(()=>refreshTranscript(id),1000);
    }catch(e){
      if(state.selected!==id||state.mode!=='chat')return;
      if(isAgentPane(id)){showError('');showTranscriptNotice(id,`读取 transcript 失败：${e.message}。会自动重试。`);retryTranscript(id,5000);return;}
      $('transcript').classList.remove('active');if(allowFallback){showError('');connect(id);}else showError(e.message);
    }finally{if(state.selected===id)syncLiveTail();}}
  // A log pane changes several times a second. Rebuilding its DOM on every frame
  // (and re-fitting the font) is what used to lock up Chrome, so paint at most
  // every CAPTURE_PAINT_MS, keep only the tail, and only re-fit on width change.
  const CAPTURE_PAINT_MS=500, CAPTURE_MAX_LINES=600;
  // A 60KB screen is already ~7000 nodes after ansi_to_html: keep the tail under
  // both a line and a byte cap so one pathological frame cannot freeze the tab.
  const CAPTURE_MAX_BYTES=160000;
  function trimCapture(content){
    let text=String(content);
    const lines=text.split('\n');
    if(lines.length>CAPTURE_MAX_LINES){text='… 只显示最近 '+CAPTURE_MAX_LINES+' 行 …\n'+lines.slice(-CAPTURE_MAX_LINES).join('\n');}
    if(text.length>CAPTURE_MAX_BYTES){
      const tail=text.slice(-CAPTURE_MAX_BYTES);
      const cut=tail.indexOf('\n');
      text='… 输出过长，已跳过前面 '+text.length+' 字节 …\n'+(cut>=0?tail.slice(cut+1):tail);
    }
    return text;
  }
  function queueCapture(content){state.capturePending=content;if(state.captureTimer)return;
    const wait=Math.max(0,CAPTURE_PAINT_MS-(performance.now()-(state.capturePainted||0)));
    state.captureTimer=setTimeout(paintCapture,wait);}
  function paintCapture(){state.captureTimer=null;const content=state.capturePending;state.capturePending=null;
    if(content==null||state.mode!=='chat'||document.hidden)return;
    const follow=state.stick&&atBottom(),oldTop=scroll.scrollTop,oldHeight=scroll.scrollHeight;
    state.capturePainted=performance.now();
    term.innerHTML=renderCapture(trimCapture(content));
    fitCapture();showError('');
    if(follow){scroll.scrollTop=scroll.scrollHeight;state.stick=true;$('jump').style.display='none';}
    else{scroll.scrollTop=oldTop+(scroll.scrollHeight-oldHeight);state.stick=false;$('jump').style.display='block';}}
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.capturePending!=null)paintCapture();});
  function fitCapture(){const p=state.panes.find(x=>x.id===state.selected);if(!p||!term.parentElement)return;const available=term.parentElement.clientWidth-44,base=7.83,columns=Math.max(p.width||120,20),ideal=Math.floor(available/columns),size=Math.max(8,Math.min(13,ideal/base*13));if(state.captureFitWidth===available&&state.captureFitSize===size)return;state.captureFitWidth=available;state.captureFitSize=size;term.style.fontSize=size+'px';}
  function connect(id){showLoadCard(id);$('transcript').classList.remove('active');$('message-shell').hidden=false;const proto=location.protocol==='https:'?'wss':'ws',ws=new WebSocket(`${proto}://${location.host}/ws/panes/${encodeURIComponent(id)}`);state.ws=ws;ws.onopen=()=>{if(state.ws!==ws||state.selected!==id||state.mode!=='chat')return;showError('');$('status-dot').className='status-dot live';$('status-text').textContent='彩色文本实时显示';};ws.onmessage=e=>{if(state.ws!==ws||state.selected!==id||state.mode!=='chat')return;const d=JSON.parse(e.data);if(d.type==='capture')queueCapture(d.content);};ws.onclose=()=>{if(state.ws!==ws||state.selected!==id||state.mode!=='chat')return;state.ws=null;$('status-dot').className='status-dot';$('status-text').textContent='重连中…';setTimeout(()=>{if(!state.livePaused&&!state.ws&&state.selected===id&&state.mode==='chat')connect(id);},1500);};ws.onerror=()=>{if(state.ws===ws&&state.selected===id&&state.mode==='chat')showError('实时连接失败，正在重试');};}
  // xterm parses on the main thread; a firehose pane can outrun it. Batch writes
  // into one rAF and drop the oldest backlog instead of growing without bound.
  const TERM_BACKLOG_MAX=1_500_000, TERM_BACKLOG_KEEP=700_000;
  const termQueues=new WeakMap();
  function pushTerminal(xterm,chunk){let q=termQueues.get(xterm);
    if(!q){q={parts:[],bytes:0,scheduled:false,ready:false};termQueues.set(xterm,q);
      xterm.write('',()=>{q.ready=true;});}
    q.parts.push(chunk);q.bytes+=chunk.length;
    if(q.bytes>TERM_BACKLOG_MAX){let dropped=0;while(q.bytes>TERM_BACKLOG_KEEP&&q.parts.length>1){const part=q.parts.shift();q.bytes-=part.length;dropped+=part.length;}
      if(dropped)q.parts.unshift('\r\n\x1b[2m… 输出过快，已跳过 '+dropped+' 字节 …\x1b[0m\r\n');}
    if(q.scheduled)return;q.scheduled=true;
    requestAnimationFrame(()=>{q.scheduled=false;if(!q.parts.length)return;const text=q.parts.join('');q.parts=[];q.bytes=0;
      if(state.xterm!==xterm)return;try{xterm.write(text);}catch{}});}
  function connectTerminal(id,reuse){
    // reuse keeps the canvas that is already on screen (pause → resume): the old screen stays visible
    // until the new attach actually starts sending, and only then is it cleared and redrawn — no black gap
    let xterm,fit,reused=false;
    if(reuse&&state.xterm&&state.fit){xterm=state.xterm;fit=state.fit;reused=true;try{fit.fit();}catch{}}
    else{
      xterm=new Terminal({cursorBlink:true,disableStdin:false,convertEol:false,scrollback:10000,fontSize:13,fontFamily:'ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace',theme:{background:'#111111',foreground:'#e7e5df',cursor:'#faf9f5',selectionBackground:'#5a574f88'}});
      fit=new FitAddon.FitAddon();
      state.xterm=xterm;state.fit=fit;xterm.loadAddon(fit);
      xterm.options.fontSize=terminalFont();
      xterm.open($('terminal-host'));
      // the row above the key bar is the soft-keyboard target; xterm's textarea only serves hardware keys
      if(isMobile())xterm.textarea?.setAttribute('inputmode','none');
      try{fit.fit();}catch{}
      // bound once per terminal, routed to whichever socket is current: rebinding on every (re)connect
      // used to stack a second handler on a reused terminal and double every keystroke
      xterm.onData(data=>{const live=state.termWs;if(live&&live.readyState===WebSocket.OPEN)live.send(JSON.stringify({type:'input',data}));});
    }

    termSizeSent=`${xterm.cols}x${xterm.rows}`;
    const proto=location.protocol==='https:'?'wss':'ws',ws=new WebSocket(`${proto}://${location.host}/ws/terminal/${encodeURIComponent(id)}?cols=${xterm.cols}&rows=${xterm.rows}`);state.termWs=ws;ws.binaryType='arraybuffer';
    const resize=()=>{if(!state.fit||state.xterm!==xterm)return;fitTerminalNow();};
    // coalesce: a keyboard transition produces a frame-by-frame stream of size changes
    state.resizeObserver=new ResizeObserver(()=>{clearTimeout(state.termFitTimer);state.termFitTimer=setTimeout(()=>requestAnimationFrame(resize),90);});
    state.resizeObserver.observe($('terminal-host'));
    let fresh=reused;   // drop the frozen screen on the first frame of the new attach, not before
    ws.onopen=()=>{showError('');$('status-dot').className='status-dot live';$('status-text').textContent='终端已连接';flushTerminalPending();requestAnimationFrame(()=>requestAnimationFrame(resize));if(!isMobile())xterm.focus();scheduleTerminalFit();};
    ws.onmessage=e=>{hideLoadCard();
      const text=typeof e.data==='string'?e.data:new TextDecoder().decode(e.data);
      if(fresh){fresh=false;const q=termQueues.get(xterm);if(q){q.parts.length=0;q.bytes=0;}try{xterm.reset();}catch{}}
      pushTerminal(xterm,text);};
    ws.onclose=()=>{if(state.selected!==id)return;if(state.livePaused){$('status-dot').className='status-dot paused';$('status-text').textContent='已暂停实时刷新（点 ▶ 继续）';return;}$('status-dot').className='status-dot';$('status-text').textContent='显示已断开';};
    ws.onerror=()=>showError('终端连接失败');

  }
  // The terminal field is a keyboard proxy, not a local buffer: whatever appears in it is
  // sent immediately and the field is emptied again. That is the only model that survives
  // editing: a mirrored buffer cannot express "cursor moved left, typed a letter" and used
  // to re-send the whole line. Backspace on the empty field sends DEL, Enter sends CR, and
  // the Ctrl/Alt keys of the key bar apply to the next character typed (Ctrl then c = ^C).
  // Typing can start before the socket is open (mode switch, reconnect): hold the bytes
  // rather than dropping them, otherwise the first character of a command vanishes.
  function sendTerminalData(data){
    if(!data)return;
    if(state.termWs?.readyState!==WebSocket.OPEN){state.termPending=(state.termPending||'')+data;return;}
    state.termWs.send(JSON.stringify({type:'input',data}));
  }
  function flushTerminalPending(){
    const pending=state.termPending||'';state.termPending='';
    if(pending&&state.termWs?.readyState===WebSocket.OPEN)state.termWs.send(JSON.stringify({type:'input',data:pending}));
  }
  function withMods(text){
    let data=text;
    const ctrl=state.terminalMods.ctrl,alt=state.terminalMods.alt;
    if(ctrl&&text.length===1&&/[a-zA-Z]/.test(text))data=String.fromCharCode(text.toUpperCase().charCodeAt(0)&31);
    else if(ctrl&&text.length===1&&/[\[\\\]^_]/.test(text))data=String.fromCharCode(text.charCodeAt(0)&31);
    if(alt)data='\x1b'+data;
    if(ctrl||alt){state.terminalMods.ctrl=false;state.terminalMods.alt=false;updateTerminalMods();}
    return data;
  }
  // iOS puts a "form assistant" bar (up/down arrows + Done) above the keyboard for any focusable
  // *form control*, and it cannot be removed from a web page. A contenteditable element is not a
  // form control, so Safari leaves that bar out -- which is what Termius looks like. `?kb=input`
  // switches back to a real <input> if some iOS build misbehaves with contenteditable.
  function useEditableCapture(){
    const field=$('terminal-input');
    if(!field||field.isContentEditable)return;
    let forced=false;
    try{forced=localStorage.getItem('tmux-kb-input')==='1';}catch{}
    if(KB_MODE==='input'||forced)return;
    const editable=document.createElement('div');
    editable.id='terminal-input';
    editable.className='terminal-kb-input';
    editable.setAttribute('contenteditable','true');
    editable.setAttribute('inputmode','text');
    editable.setAttribute('enterkeyhint','send');
    editable.setAttribute('autocapitalize','off');
    editable.setAttribute('autocorrect','off');
    editable.setAttribute('spellcheck','false');
    editable.setAttribute('aria-hidden','true');
    field.replaceWith(editable);
  }
  // iOS attaches a native accessory bar to the web view whenever an editable element is focused;
  // it cannot be hidden from the page, so on iOS the keyboard is only summoned on purpose.
  // read once: choosing a pane rewrites the URL to '/', which would drop the flag
  const KB_MODE=new URLSearchParams(location.search).get('kb')||'';
  function keyboardOnTerminalTap(){
    const forced=KB_MODE;
    if(forced==='auto')return true;
    if(forced==='manual')return false;
    const ios=/iP(hone|ad|od)/.test(navigator.userAgent)
      ||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
    return !ios;
  }
  function setupTerminalInput(){
    const field=$('terminal-input');
    if(!field)return;
    const editable=field.isContentEditable;
    const read=()=>editable?field.textContent:field.value;
    const clear=()=>{
      if(!editable){field.value='';return;}
      field.textContent='';
      // keep the caret at the end, otherwise iOS starts typing in front of old text
      const range=document.createRange();
      range.selectNodeContents(field);
      range.collapse(false);
      const selection=getSelection();
      if(selection){selection.removeAllRanges();selection.addRange(range);}
    };
    let composing=false;
    const flush=()=>{                       // send what the keyboard produced, then empty the box
      const value=read();
      if(!value)return;
      clear();
      sendTerminalData(withMods(value));
    };
    field.addEventListener('compositionstart',()=>{composing=true;});
    field.addEventListener('compositionend',()=>{composing=false;flush();});
    field.addEventListener('input',()=>{if(!composing)flush();});
    // a contenteditable does not fire `input` for a backspace on an empty element
    field.addEventListener('beforeinput',event=>{
      if(event.inputType!=='deleteContentBackward')return;
      if(read())return;
      event.preventDefault();
      sendTerminalData(withMods('\x7f'));
    });
    field.addEventListener('keydown',event=>{
      if(event.isComposing||composing)return;
      const meta=event.metaKey,ctrl=event.ctrlKey||state.terminalMods.ctrl,alt=event.altKey||state.terminalMods.alt;
      const named=terminalSequences[event.key];
      if(meta)return;                                   // leave Cmd/Ctrl shortcuts alone
      if(ctrl&&(event.key==='v'||event.key==='V'))return; // paste: the input event delivers it
      if(event.key==='Enter'){event.preventDefault();clear();sendTerminalData(withMods('\r'));return;}
      if(event.key==='Backspace'){if(!read()){event.preventDefault();sendTerminalData(withMods('\x7f'));}return;}
      if(named&&!ctrl&&!alt){event.preventDefault();sendTerminalData(withMods(named));return;}   // arrows, Tab, Esc, PgUp/PgDn, Home/End
      if((ctrl||alt)&&event.key.length===1){event.preventDefault();sendTerminalData(withMods(event.key));return;}
    });
    field.addEventListener('focus',()=>setComposing(true));
    field.addEventListener('blur',()=>setTimeout(()=>setComposing(keyboardWanted()),120));
    // Tapping the terminal brings up the keyboard -- except on iOS, where the system keyboard drags
    // along a native "↑ ↓ Done" accessory bar that no web page can remove (WKFormAccessoryView).
    // There the keyboard is opt-in: tap the keyboard key in the bar when you want to type.
    // xterm stops propagation on its own element, so listen in the capture phase
    $('terminal-host')?.addEventListener('pointerdown',event=>{
      if(!isMobile())return;
      if(window.getSelection&&String(window.getSelection()))return;   // selecting text, don't fight it
      if(event.target.closest?.('#terminal-jump'))return;
      if(!keyboardOnTerminalTap()){
        if(kbCustom())toggleTypePad(true);
        else field.focus({preventScroll:true});
        return;
      }
      field.focus({preventScroll:true});
    },true);
    // some engines deliver the touch before any pointer event; keep both paths
    $('terminal-host')?.addEventListener('touchstart',event=>{
      if(!isMobile()||event.touches.length!==1)return;
      if(window.getSelection&&String(window.getSelection()))return;
      if(!keyboardOnTerminalTap()){
        if(kbCustom())toggleTypePad(true);
        else field.focus({preventScroll:true});
        return;
      }
      field.focus({preventScroll:true});
    },{capture:true,passive:true});
  }
  const terminalSequences={Escape:'\x1b',Tab:'\t',BackTab:'\x1b[Z',Enter:'\r',ArrowUp:'\x1b[A',ArrowDown:'\x1b[B',ArrowRight:'\x1b[C',ArrowLeft:'\x1b[D',PageUp:'\x1b[5~',PageDown:'\x1b[6~',Home:'\x1b[H',End:'\x1b[F',Delete:'\x1b[3~'};
  // the browser sends 'Backspace' for both; DEL and BS are different keys to a terminal
  function updateTerminalMods(){document.querySelectorAll('[data-mod]').forEach(button=>button.classList.toggle('active',!!state.terminalMods[button.dataset.mod]));}
  function sendTerminalKey(key){if(state.mode!=='terminal'||state.termWs?.readyState!==WebSocket.OPEN)return;let data=terminalSequences[key]||key;if(state.terminalMods.ctrl&&key.length===1)data=String.fromCharCode(key.toUpperCase().charCodeAt(0)&31);if(state.terminalMods.alt)data='\x1b'+data;state.termWs.send(JSON.stringify({type:'input',data}));state.terminalMods.ctrl=false;state.terminalMods.alt=false;updateTerminalMods();}
  // ---- our own keyboard: the system one brings iOS' "up/down/Done" bar along, this does not.
  const KEY_LAYOUTS={
    lower:[['q','w','e','r','t','y','u','i','o','p'],
           ['a','s','d','f','g','h','j','k','l'],
           ['z','x','c','v','b','n','m']],
    upper:[['Q','W','E','R','T','Y','U','I','O','P'],
           ['A','S','D','F','G','H','J','K','L'],
           ['Z','X','C','V','B','N','M']],
    symbols:[['1','2','3','4','5','6','7','8','9','0'],
             ['-','/',':',';','(',')','$','&','@','%'],
             ['.',',','?','!','#','*','+','=','_','~']]
  };
  function kbCustom(){
    const forced=KB_MODE;
    if(forced==='custom')return true;
    if(forced==='system')return false;
    try{
      const saved=localStorage.getItem('tmux-kb-custom');
      if(saved==='1')return true;
      if(saved==='0')return false;
    }catch{}
    const ios=/iP(hone|ad|od)/.test(navigator.userAgent)
      ||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
    return ios;
  }
  function renderTypePad(){
    const pad=$('terminal-type');
    if(!pad)return;
    const layout=state.kbSymbols?'symbols':(state.kbShift?'upper':'lower');
    const rows=KEY_LAYOUTS[layout];
    let html='';
    rows.forEach((row,index)=>{
      html+='<div class="tk-row">';
      if(index===2)html+='<button class="terminal-key action wide" type="button" data-type-key="shift">shift</button>';
      for(const character of row){
        const escaped=character.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        html+=`<button class="terminal-key" type="button" data-type-char="${escaped}">${escaped}</button>`;
      }
      if(index===0)html+='<button class="terminal-key action" type="button" data-type-key="back" aria-label="退格">⌫</button>';
      if(index===1)html+='<button class="terminal-key action" type="button" data-type-key="space" aria-label="空格">␣</button><button class="terminal-key action" type="button" data-type-key="next" aria-label="下一组按键"><svg class="ico" aria-hidden="true"><use href="#i-chevron-left"/></svg></button>';
      if(index===2)html+='<button class="terminal-key action wide" type="button" data-type-key="back">del</button>';
      html+='</div>';
    });
    html+='<div class="tk-row"><button class="terminal-key action" type="button" data-type-key="previous" aria-label="上一组按键"><svg class="ico" aria-hidden="true"><use href="#i-chevron-left"/></svg></button>'
      + `<button class="terminal-key action" type="button" data-type-key="digits">${state.kbSymbols?'abc':'123'}</button>`
      + '<button class="terminal-key action" type="button" data-mod="ctrl">ctrl</button>'
      + '<button class="terminal-key action" type="button" data-mod="alt">alt</button>'
      + '<button class="terminal-key grow" type="button" data-type-key="space">空格</button>'
      + '<button class="terminal-key action" type="button" data-type-key="enter">ret</button>'
      + '<button class="terminal-key action wide" type="button" data-type-key="system">系统键盘</button>'
      + '</div>';
    pad.innerHTML=html;
    updateTerminalMods();
  }
  function typePadSend(text){
    if(!text)return;
    sendTerminalData(withMods(text));
  }
  function toggleTypePad(open){
    const keys=$('terminal-keys');
    if(!keys)return;
    const wanted=open===undefined?!keys.classList.contains('type-open'):!!open;
    keys.classList.toggle('type-open',wanted);
    keys.classList.remove('ext-open');
    $('terminal-more').setAttribute('aria-pressed','false');
    $('terminal-kb').setAttribute('aria-pressed',String(wanted));
    if(wanted){
      $('terminal-input')?.blur();                     // never both keyboards at once
      renderTypePad();
      $('terminal-type').scrollLeft=0;
    }
    scheduleTerminalFit();
  }
  function setupTypePad(){
    const pad=$('terminal-type');
    if(!pad)return;
    let repeatTimer=null,repeatDelay=null;
    const stopRepeat=()=>{clearTimeout(repeatDelay);clearInterval(repeatTimer);repeatDelay=null;repeatTimer=null;};
    let repeated=false;
    pad.addEventListener('pointerdown',event=>{
      stopRepeat();
      repeated=false;
      if(!event.target.closest('[data-type-key="back"]'))return;
      repeatDelay=setTimeout(()=>{
        repeated=true;
        typePadSend('\x7f');
        repeatTimer=setInterval(()=>typePadSend('\x7f'),70);
      },380);
    });
    pad.addEventListener('click',event=>{
      const key=event.target.closest('[data-type-key],[data-type-char]');
      if(!key)return;
      event.preventDefault();
      const character=key.dataset.typeChar;
      if(character!==undefined){
        typePadSend(character);
        if(state.kbShift&&!state.kbSymbols){state.kbShift=false;renderTypePad();}
        return;
      }
      switch(key.dataset.typeKey){
        case 'shift':state.kbSymbols=false;state.kbShift=!state.kbShift;renderTypePad();pad.scrollLeft=0;break;
        case 'digits':state.kbSymbols=!state.kbSymbols;state.kbShift=false;renderTypePad();pad.scrollLeft=0;break;
        case 'space':typePadSend(' ');break;
        case 'enter':typePadSend('\r');break;
        case 'back':if(!repeated || event.detail===0)typePadSend('\x7f');break;
        case 'next':pad.scrollLeft=pad.scrollWidth;pad.querySelector('[data-type-key="previous"]').focus({preventScroll:true});break;
        case 'previous':pad.scrollLeft=0;pad.querySelector('[data-type-key="next"]').focus({preventScroll:true});break;
        case 'system':
          try{localStorage.setItem('tmux-kb-custom','0');}catch{}
          toggleTypePad(false);
          $('terminal-input')?.focus({preventScroll:true});
          break;
      }
    });
    pad.addEventListener('pointerup',stopRepeat);
    pad.addEventListener('pointercancel',stopRepeat);
    pad.addEventListener('pointerleave',stopRepeat);
  }
  function setupTerminalKeyboardButton(){
    const button=$('terminal-kb'),field=$('terminal-input');
    if(!button||!field)return;
    const sync=()=>button.setAttribute('aria-pressed',String(document.activeElement===field || $('terminal-keys').classList.contains('type-open')));
    button.addEventListener('click',()=>{
      if(kbCustom()){
        toggleTypePad();
        setTimeout(sync,60);
        return;
      }
      if(document.activeElement===field)field.blur();
      else field.focus({preventScroll:true});
      setTimeout(sync,60);
    });
    field.addEventListener('focus',sync);
    field.addEventListener('blur',()=>setTimeout(sync,60));
  }
  async function pasteIntoTerminal(){
    try{
      const text=await navigator.clipboard.readText();
      if(text)sendTerminalData(text.replace(/\r?\n/g,'\r'));
    }catch{showError('浏览器不允许直接读剪贴板，请长按粘贴');}
  }
  function setupTerminalControls(){
    const keys=$('terminal-keys');
    // Keep the input focus on taps. Dispatch only the completed click so a swipe cannot type.
    keys.addEventListener('pointerdown',e=>{if(e.target.closest('.terminal-key'))e.preventDefault();});
    keys.addEventListener('click',e=>{
      const button=e.target.closest('.terminal-key');
      if(!button)return;
      e.preventDefault();
      if(button.dataset.keyGroup){
        for(const tab of keys.querySelectorAll('[data-key-group]')){
          const selected=tab===button;
          tab.setAttribute('aria-pressed',String(selected));
          $(tab.getAttribute('aria-controls')).hidden=!selected;
        }
        $('terminal-ext').scrollLeft=0;
        return;
      }
      if(button.dataset.mod){
        const mod=button.dataset.mod;state.terminalMods[mod]=!state.terminalMods[mod];updateTerminalMods();
      }else if(button.dataset.text){
        sendTerminalData(withMods(button.dataset.text));
      }else if(button.dataset.ctrlKey){
        sendTerminalData(String.fromCharCode(button.dataset.ctrlKey.toUpperCase().charCodeAt(0)&31));
      }else if(button.dataset.key){
        sendTerminalKey(button.dataset.key);
      }else if(button.dataset.paste){
        // 扩展键盘里的 paste 按钮（原来只有被删掉的那份重复处理器认它）
        pasteIntoTerminal();
      }
    });
    $('terminal-paste')?.addEventListener('click',pasteIntoTerminal);
    $('terminal-more')?.addEventListener('click',()=>{
      const keys=$('terminal-keys');
      if(keys.classList.contains('type-open'))toggleTypePad(false);
      const open=keys.classList.toggle('ext-open');
      $('terminal-more').setAttribute('aria-pressed',String(open));
      // the extended pad replaces the soft keyboard (that is how it fits): hide the keyboard first
      if(open)$('terminal-input')?.blur();
      else if(!kbCustom())$('terminal-input')?.focus({preventScroll:true});
      scheduleTerminalFit();
    });
    // 这里以前还有一份 #terminal-ext 的 pointerdown 处理器 —— 而 #terminal-ext 就在
    // #terminal-keys 里面，同一个按键会先命中它、再冒泡到上面那个委托处理器，
    // 一次点击发两遍（↑ 每次都跳两行）。ext 的按键本来就由上面那个处理器覆盖（含 paste 的单独 click），
    // 所以整块删掉，只留一处出口。
    setupTerminalKeyboardButton();
    setupTypePad();
    setupTerminalZoom();
    setupTerminalJump();
  }
  // 这里原来还有一段"按住 280ms 后滑动换方向键"的 pad 逻辑，指向 #terminal-pad ——
  // 那个元素在 index.html 里根本不存在，那段代码永远 return，是死代码。已删。
  // ---- pinch to zoom the terminal font (the column count follows), remembered per browser
  function terminalFont(){const saved=Number(localStorage.getItem('tmux-term-font'));return saved>=9&&saved<=22?saved:13;}
  function setTerminalFont(size){
    const clamped=Math.max(9,Math.min(22,Math.round(size)));
    if(!state.xterm||state.xterm.options.fontSize===clamped)return;
    state.xterm.options.fontSize=clamped;
    localStorage.setItem('tmux-term-font',String(clamped));
    try{state.fit?.fit();}catch{}
    notifyTermSize();
  }
  function setupTerminalZoom(){
    const host=$('terminal-host');
    if(!host)return;
    let startDistance=0,startFont=13;
    const distance=touches=>Math.hypot(touches[0].clientX-touches[1].clientX,touches[0].clientY-touches[1].clientY);
    host.addEventListener('touchstart',event=>{
      if(event.touches.length!==2)return;
      startDistance=distance(event.touches);
      startFont=state.xterm?state.xterm.options.fontSize||13:13;
    },{passive:true});
    host.addEventListener('touchmove',event=>{
      if(event.touches.length!==2||!startDistance)return;
      const ratio=distance(event.touches)/startDistance;      // always measured from the pinch start
      if(Math.abs(ratio-1)<0.05)return;
      setTerminalFont(startFont*ratio);
    },{passive:true});
    host.addEventListener('touchend',()=>{startDistance=0;},{passive:true});
  }
  function setupTerminalJump(){
    const button=$('terminal-jump');
    if(!button)return;
    const sync=()=>{
      if(state.mode!=='terminal'||!state.xterm)return;
      const viewport=state.xterm.element?.querySelector('.xterm-viewport');
      if(!viewport)return;
      const away=viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight>40;
      button.hidden=!away;
    };
    button.addEventListener('click',()=>{
      try{state.xterm?.scrollToBottom();state.xterm?.refresh(0,Math.max(0,(state.xterm?.rows||1)-1));}catch{}
      button.hidden=true;
    });
    setInterval(sync,700);
  }
  function setMode(mode){if(state.mode===mode)return;closeChatReply();state.mode=mode;if(state.selected)replaceConsoleQuery(params=>params.set('mode',mode));syncNativeForm();window.TmuxPrompt?.refresh();document.querySelector('.app').classList.toggle('terminal-view',mode==='terminal');$('chat-mode').classList.toggle('active',mode==='chat');$('terminal-mode-button').classList.toggle('active',mode==='terminal');$('chat-mode').setAttribute('aria-pressed',String(mode==='chat'));$('terminal-mode-button').setAttribute('aria-pressed',String(mode==='terminal'));$('chat-mode').title=mode==='chat'?'聊天视图（当前）':'聊天视图';$('terminal-mode-button').title=mode==='terminal'?'终端视图（当前）':'终端视图';closeConnections();if(state.selected)(mode==='terminal'?connectTerminal(state.selected):connectTranscript(state.selected,true));syncEarlierBar();syncProgress();if(mode==='chat'&&state.selected&&canAutoFocusComposer())input.focus();syncFullscreen();localStorage.setItem('tmux-web-mode',mode);if(mode!=='terminal'){$('terminal-input')?.blur();toggleTypePad(false);}}
  function isFullscreen(){return document.fullscreenElement===$('main')||$('main').classList.contains('fullscreen-fallback');}
  function syncFullscreen(){const active=isFullscreen();const label=$('fullscreen').querySelector('span');if(label)label.textContent=active?'退出全屏':(state.mode==='terminal'?'终端全屏':'对话全屏');setTimeout(()=>{if(state.fit&&state.termWs?.readyState===WebSocket.OPEN){state.fit.fit();state.termWs.send(JSON.stringify({type:'resize',cols:state.xterm.cols,rows:state.xterm.rows}));}},80);}
  async function toggleFullscreen(){const main=$('main'),app=document.querySelector('.app');if(isFullscreen()){if(document.fullscreenElement)await document.exitFullscreen();main.classList.remove('fullscreen-fallback');app.classList.remove('fullscreen-fallback');}else if(main.requestFullscreen){try{await main.requestFullscreen();}catch{main.classList.add('fullscreen-fallback');app.classList.add('fullscreen-fallback');}}else{main.classList.add('fullscreen-fallback');app.classList.add('fullscreen-fallback');}syncFullscreen();}
  let sendStateTimer=0;
  function setSendState(text,kind){
    const el=$('send-state');if(!el)return;
    clearTimeout(sendStateTimer);
    el.textContent=text||'';
    el.className='send-state'+(kind?' '+kind:'');
    if(kind==='ok'&&text)sendStateTimer=setTimeout(()=>{el.textContent='';el.className='send-state';},1400);
  }
  function sendResultState(result){
    if(result&&result.status==='pasted_not_submitted')return {text:'已粘贴但未提交',kind:'bad'};
    if(result&&(result.status==='retrying'||result.retrying))return {text:'正在重试提交…',kind:'retrying'};
    return {text:'已发送 ✓',kind:'ok'};
  }
  // 后台重试的终态 → 乐观气泡该变成什么：还在重试不用动，提交了变已发送，
  // 到上限或被新发送顶掉都按未提交显示（旧文字已被新发送的 C-u 清掉，
  // 本来也没提交，显示未提交是实话）。
  function resendAction(resend){
    if(!resend||!resend.status)return null;
    if(resend.status==='submitted')return 'sent';
    if(resend.status==='failed'||resend.status==='superseded')return 'failed';
    return null;
  }
  function syncResendState(d){
    const action=resendAction(d&&d.resend);
    if(!action)return;
    for(const [localId,pending] of state.pendingMessages){
      if(pending.pane!==state.selected||pending.status!=='retrying')continue;
      setOptimisticStatus(localId,action);
      if(action==='failed')setSendState('已粘贴但未提交','bad');
    }
  }

  async function resendPending(localId){
    const pending=state.pendingMessages.get(localId);
    if(!pending)return;
    pending.status='sending';
    setOptimisticStatus(localId,'sending');
    setSendState('重发中…');
    try{
      const d=await api(`/api/panes/${encodeURIComponent(pending.pane)}/input`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:pending.text,submit:pending.submit!==false})});
      const st=sendResultState(d);
      setOptimisticStatus(localId,st.kind==='bad'?'failed':st.kind==='retrying'?'retrying':'sent');
      if(st.kind==='bad')setSendState(st.text,st.kind);else if(st.kind==='retrying')setSendState(st.text,st.kind);else setSendState('已发送 ✓','ok');
      showError('');
    }catch(e){
      setOptimisticStatus(localId,'failed');
      setSendState('发送失败','bad');
      showError(e.message);
    }
  }
  async function sendInput(){
    // 附件先上传到 <pane 目录>/upload_files，消息前面补一行 "user uploaded file “a & b”"
    const prompt=attachmentsPrompt();
    const typed=input.value;
    const text=prompt?(typed.trim()?`${prompt}\n\n${typed}`:prompt):typed;
    const pane=state.selected;
    if(!pane||!text.trim()||attachmentsPending())return;
    // 条带在按 Enter 这一刻就收起来（以前等 POST 回来才清，那一秒图片还留着）
    // agent 正忙：排到服务端队列，等这一轮结束自动发（以前是直接塞进去，容易被吞）
    if(!prompt&&state.waiting){
      try{
        const data=await enqueueMessage(text);
        input.value='';input.style.height='auto';send.disabled=!state.selected;
        setSendState(`已排队（${data.pending}）`,'ok');
        toast('已排队，这一轮结束会自动发出',3000);
        if(!isMobile())input.focus();
      }catch(error){showError(error.message);setSendState('排队失败','bad');}
      return;
    }
    const attachments=prompt?takeAttachments():[];
    const localId=addOptimisticMessage(text);input.value='';input.style.height='auto';send.disabled=true;setSendState('发送中…');try{const d=await api(`/api/panes/${encodeURIComponent(pane)}/input`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,submit:true})});const st=sendResultState(d);setOptimisticStatus(localId,st.kind==='bad'?'failed':st.kind==='retrying'?'retrying':'sent');setSendState(st.text,st.kind);showError('');discardAttachments(attachments);if(!isMobile())input.focus();state.stick=true;refreshTranscript(pane,{force:true});}catch(e){setOptimisticStatus(localId,'failed');showError(e.message);setSendState('发送失败，点消息上的重发','bad');
    // 不把原文放回输入框：这条消息已经在聊天里显示出来了（带「重发」），
    // 再放一份到输入框，用户会以为要重打一遍，也容易连发两条相同的。
    if(!isMobile())input.focus();
  }finally{send.disabled=!state.selected;}}
  // @ inserts a relay handle into the current draft. / inserts a skill or prompt.
  function inputCommands(){return [
    {key:'jixu',label:'继续',text:'继续'},
    {key:'zongjie',label:'总结',text:'总结一下目前的进展：已完成什么、正在做什么、还剩什么'},
    {key:'jiancha',label:'检查',text:'检查一下有没有错误或遗漏，有问题先说清楚再改'},
    {key:'xiubaocuo',label:'修报错',text:'这个报错是什么原因？先定位根因再修，不要只改表面'},
    {key:'jieshi',label:'解释',text:'解释一下这段代码是做什么的，关键逻辑讲清楚'},
    {key:'zicha',label:'自查',text:'提交前自查一遍：测试跑过没有、有没有多余改动、有没有漏掉的需求'},
  ];}
  function matchInputCompletion(text,cursor){
    // 光标前的当前词：@ 或 / 开头，前面是行首或空白，中间无空白。
    // 邮箱（a@b）、路径（http://x）、算式（3/4）里的 @ / 前面不是
    // 空白，不触发；词后打了空格也视为已放弃补全。
    const value=String(text==null?'':text),at=cursor==null?value.length:cursor;
    const before=value.slice(0,at);
    const match=/(^|\s)([@\/])([^\s@\/]*)$/.exec(before);
    if(!match)return null;
    return {kind:match[2]==='@'?'pane':'command',start:match.index+match[1].length,query:match[3]||''};
  }
  function filterPaneCompletions(panes,query){
    const q=String(query||'').trim().toLowerCase();
    const matches=(panes||[]).filter(p=>p?.id && p.number).filter(p=>{
      if(!q)return true;
      const hay=[p.title,p.target,p.command,p.id,paneLabel(p),p.number==null?'':String(p.number)].join(' ').toLowerCase();
      return hay.includes(q);
    });
    if(/^#?\d+$/.test(q)){
      const exact=p=>String(p.number)===q.replace(/^#/,'');
      return [...matches.filter(exact),...matches.filter(p=>!exact(p))].slice(0,8);
    }
    return matches.slice(0,8);
  }
  function filterCommandCompletions(query){
    const q=String(query||'').trim().toLowerCase();
    return inputCommands().filter(c=>!q||c.label.toLowerCase().includes(q)||c.key.includes(q));
  }
  const skillPrefixes={codex:'$',hermes:'/skill ',claude:'/',kimi:'/skill:'};
  function installedSkills(data){
    const prefix=skillPrefixes[data?.provider];
    if(typeof prefix!=='string' || !Array.isArray(data?.skills))return null;
    const names=data.skills.slice(0,512).filter(name=>typeof name==='string' &&
      /^[\p{L}\p{N}_][\p{L}\p{N}_.-]{0,95}$/u.test(name));
    return {provider:data.provider,skills:names};
  }
  function showSkillCatalog(catalog,data){
    const prefix=skillPrefixes[data.provider];
    catalog.data=data;
    catalog.inline=data.provider==='codex';
    catalog.items=data.skills.map(name=>({kind:'skill',label:name,text:prefix+name+' ',sub:'Skill · '+prefix+name}));
    catalog.hint=catalog.items.length?'':'未发现已安装的技能';
  }
  function skillSuggestions(query,atStart){
    const pane=state.panes.find(p=>p.id===state.selected);
    if(!pane || !isAgentPane(pane.id))return {items:[],hint:''};
    const key=JSON.stringify([pane.id,pane.pane_pid||'',pane.path||'',pane.provider||'',pane.command||'']);
    let catalog=state.completionSkills;
    if(!catalog || catalog.key!==key){
      catalog={key,items:[],data:null,hint:'正在读取技能…',expires:0};
      state.completionSkills=catalog;
      const cached=installedSkills(window.TmuxPaneCache?.readSkills(key));
      if(cached)showSkillCatalog(catalog,cached);
    }
    if(Date.now()>=catalog.expires){
      catalog.expires=Infinity;
      const oldHint=catalog.hint, oldData=catalog.data;
      api('/api/panes/'+encodeURIComponent(pane.id)+'/skills').then(result=>{
        if(state.completionSkills!==catalog)return;
        const data=installedSkills(result);
        if(!data)throw new Error('无效的技能目录');
        if(!oldData || oldData.provider!==data.provider || oldData.skills.length!==data.skills.length ||
           oldData.skills.some((name,i)=>name!==data.skills[i]))showSkillCatalog(catalog,data);
        catalog.hint=catalog.items.length?'':'未发现已安装的技能';
        window.TmuxPaneCache?.saveSkills(key,data);
        catalog.expires=Date.now()+15000;
      }).catch(error=>{
        if(state.completionSkills!==catalog)return;
        if([401,403,404].includes(error.status)){
          catalog.items=[];catalog.data=null;
          window.TmuxPaneCache?.saveSkills(key,null);
        }
        catalog.hint=catalog.data?'暂时无法更新，显示已缓存技能':'技能目录暂时无法读取';
        catalog.expires=Date.now()+5000;
      }).finally(()=>{
        if((oldData!==catalog.data || oldHint!==catalog.hint) && state.completionSkills===catalog &&
           state.selected===pane.id && completeMatch?.kind==='command' && document.activeElement===input)
          renderInputComplete(true);
      });
    }
    const q=query.toLowerCase();
    return {items:(atStart || catalog.inline)?catalog.items.filter(item=>
      item.label.toLowerCase().includes(q) || item.text.trim().slice(1).toLowerCase().startsWith(q)).slice(0,40):[],
      hint:catalog.hint};
  }
  function applyCompletion(text,cursor,match,replacement){
    // 把 [match.start, cursor) 的 token 换成 replacement，返回新文本与新光标。
    const value=String(text==null?'':text),at=cursor==null?value.length:cursor;
    const next=value.slice(0,match.start)+replacement+value.slice(at);
    return {text:next,cursor:match.start+replacement.length};
  }
  let completeMatch=null,completeItems=[],completeIndex=0,completeOpen=false,completeSuppressed=false;
  function closeInputComplete(){
    completeMatch=null;completeItems=[];completeIndex=0;completeOpen=false;
    const box=$('input-complete');if(box)box.hidden=true;
  }
  function paintCompleteSelection(){
    const box=$('input-complete');if(!box)return;
    box.querySelectorAll('[data-complete-index]').forEach(node=>{
      node.classList.toggle('selected',Number(node.dataset.completeIndex)===completeIndex);
      node.setAttribute('aria-selected',String(Number(node.dataset.completeIndex)===completeIndex));
    });
  }
  function completeMove(step){
    if(!completeItems.length)return;
    $('input-complete').dataset.navigated='1';
    completeIndex=(completeIndex+step+completeItems.length)%completeItems.length;
    paintCompleteSelection();
    $('input-complete').children[completeIndex]?.scrollIntoView({block:'nearest'});
  }
  function renderInputComplete(keepSelection=false){
    const box=$('input-complete');if(!box){completeOpen=false;return;}
    if(!completeMatch||completeSuppressed){box.hidden=true;completeOpen=false;return;}
    const skills=completeMatch.kind==='command'
      ?skillSuggestions(completeMatch.query,completeMatch.start===0):{items:[],hint:''};
    const previous=keepSelection && box.dataset.navigated==='1' && completeItems[completeIndex];
    const items=completeMatch.kind==='pane'
      ?filterPaneCompletions(state.panes,completeMatch.query).map(p=>({kind:'pane',id:p.id,text:'@'+paneLabel(p).replace(/^#/, '')+' ',label:p.title||p.target||p.id,sub:`${paneLabel(p)}${p.command?' · '+p.command:''}`}))
      :skills.items.concat(filterCommandCompletions(completeMatch.query).map(c=>({kind:'command',label:c.label,text:c.text,sub:c.text})));
    completeItems=items;completeIndex=previous?Math.max(0,items.findIndex(item=>item.text===previous.text)):0;
    if(!keepSelection)box.dataset.navigated='';
    if(!items.length && !skills.hint){box.hidden=true;completeOpen=false;return;}
    box.innerHTML=items.map((item,i)=>`<button type="button" class="complete-item${i===completeIndex?' selected':''}" data-complete-index="${i}" role="option" aria-selected="${i===completeIndex}"><span class="complete-label">${esc(item.label)}</span><span class="complete-sub">${esc(item.sub||'')}</span></button>`).join('');
    if(skills.hint){const status=document.createElement('div');status.className='complete-sub';status.textContent=skills.hint;box.append(status);}
    box.hidden=false;completeOpen=items.length>0;
    if(previous)box.children[completeIndex]?.scrollIntoView({block:'nearest'});
  }
  function refreshInputComplete(fromTyping){
    if(fromTyping)completeSuppressed=false;
    if(completeSuppressed){closeInputComplete();return;}
    const pos=input.selectionStart;
    if(pos==null){closeInputComplete();return;}
    const match=matchInputCompletion(input.value,pos);
    // Cursor events following a handled arrow key must keep the active row.
    if(!fromTyping && completeOpen && match && match.start===completeMatch.start &&
       match.kind===completeMatch.kind && match.query===completeMatch.query)return;
    completeMatch=match;
    renderInputComplete();
  }
  function acceptInputComplete(item){
    const match=completeMatch;if(!match||!item)return;
    const done=applyCompletion(input.value,input.selectionStart,match,item.text);
    closeInputComplete();
    input.value=done.text;
    input.dispatchEvent(new Event('input'));
    input.setSelectionRange(done.cursor,done.cursor);
    if(!isMobile())input.focus();
  }
  async function createShare(e){e.preventDefault();const result=$('share-result');result.textContent='创建中…';try{const d=await api('/api/shares',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pane_id:state.selected,slug:$('share-slug').value.trim(),password:$('share-password').value,expires_in:Number($('share-expiry').value),allow_input:$('share-allow-input').checked,allow_model_switch:$('share-allow-model').checked,allow_terminal:$('share-allow-terminal').checked})});const url=new URL(d.url,location.origin).href;try{await navigator.clipboard.writeText(url);result.textContent=`已创建并复制：${url}`;}catch{result.textContent=`已创建：${url}`;}}catch(error){result.textContent=error.message;}}
  list.addEventListener('click',e=>{const add=e.target.closest('[data-new-session]');if(!add)return;
    e.preventDefault();e.stopPropagation();state.sessionHost=add.dataset.newSession;
    openSessionDialog(add.dataset.newSession,add.dataset.sessionPath||'');},true);
  // 「最近」块里的星和树上的星是同一份收藏：谁改了都要通知另一边
  window.addEventListener('tmuxweb:favorites',()=>{loadFavorites();});
  list.addEventListener('toggle',e=>{const d=e.target.closest('details[data-tree]');if(!d)return;d.open?state.treeOpen.add(d.dataset.tree):state.treeOpen.delete(d.dataset.tree);saveTree();},true);list.onclick=e=>{const renameSession=e.target.closest('[data-rename-session]');
    if(renameSession){e.preventDefault();e.stopPropagation();
      renameTmuxSession(renameSession.dataset.renameHost,renameSession.dataset.renameSession);return;}
    const renameWindow=e.target.closest('[data-rename-window]');
    if(renameWindow){e.preventDefault();e.stopPropagation();
      renameTmuxWindow(renameWindow.dataset.renameWindow,renameWindow.dataset.renameValue);return;}
    const star=e.target.closest('[data-star]');if(star){e.preventDefault();e.stopPropagation();toggleFavorite(star.dataset.star);return;}const refresh=e.target.closest('[data-refresh-host]');if(refresh){e.preventDefault();e.stopPropagation();refreshHost(refresh.dataset.refreshHost,refresh);return;}const b=e.target.closest('[data-pane]');if(b){selectPane(b.dataset.pane);if(isMobile())closeSidebar();}};function saveTree(){try{localStorage.setItem('tmux-tree-open',JSON.stringify([...state.treeOpen]));}catch{}}function treeKeys(){const keys=[];for(const host of state.hosts||[]){keys.push(host.id);for(const p of host.panes||[]){keys.push(`${host.id}/${p.session}`);keys.push(`${host.id}/${p.session}/${p.window}:${p.window_name||''}`);}}return keys;}$('tree-expand').onclick=()=>{state.treeOpen=new Set(treeKeys());saveTree();renderList();};$('tree-collapse').onclick=()=>{state.treeOpen=new Set();saveTree();renderList();};
  function contentSnippet(id){
    const hit=contentHits.get(id);
    return hit?'<div class="pane-search-snippet">'+(hit.cached?'浏览器缓存':hit.source==='screen'?'终端':'聊天')+' · '+esc(hit.snippet)+'</div>':'';
  }
  function searchCandidates(terms){
    const candidates=[];
    for(const host of state.hosts){
      for(const pane of host.panes||[]){
        const hay=paneSearchHaystack(pane,host,state.needsInput?.get(pane.id));
        if(terms.some(term=>/^#\d+$/.test(term)&&term!=='#'+pane.number))continue;
        const missing=terms.filter(term=>!hay.includes(term));
        if(!missing.length)continue;
        candidates.push({pane:pane.id,terms:missing,number:pane.number||0,pid:pane.pane_pid,host:host.name||host.id});
      }
    }
    return candidates.sort((a,b)=>b.number-a.number);
  }
  const searchRoutes={server_cache:'服务端缓存',local:'本机',agent:'Agent',federation:'联邦',ssh:'SSH 回退',shared:'复用读取',online:'在线'};
  function paintSearchProgress(run){
    const el=$('pane-search-progress');el.hidden=!run;
    if(!run)return;
    const seconds=((performance.now()-run.started)/1000).toFixed(1);
    const via=Object.entries(run.routes).map(([key,n])=>(searchRoutes[key]||key)+' '+n);
    const details=['浏览器缓存命中 '+run.browser,...via].join(' · ');
    el.textContent=run.stage+' · 正文 '+run.done+'/'+run.total+' · '+seconds+' 秒\n'+details;
  }
  function browserMatch(candidate){
    const memory=state.transcripts.get(candidate.pane);
    const doc=memory?.messages?.length?memory:window.TmuxPaneCache?.readTranscript(candidate.pane,7*24*3600000);
    if(!doc||!doc.messages?.length)return null;
    if(candidate.pid&&doc.panePid&&String(candidate.pid)!==String(doc.panePid))return null;
    const age=Date.now()-(doc.at||doc.cachedAt||0);
    if(age<0||age>7*24*3600000)return null;
    for(let i=doc.messages.length-1;i>=Math.max(0,doc.messages.length-500);i--){
      const text=doc.messages[i].text;
      if(typeof text!=='string')continue;
      const folded=text.toLowerCase();
      if(!candidate.terms.every(t=>folded.includes(t)))continue;
      const offset=Math.max(0,folded.indexOf(candidate.terms[0])-35);
      const snippet=(offset?'…':'')+text.slice(offset,offset+160).replace(/\s+/g,' ')+(offset+160<text.length?'…':'');
      return {snippet,cached:true,fresh:Boolean(candidate.pid&&doc.panePid&&age<25000)};
    }
    return null;
  }
  async function fetchSearchBatch(batch,run,cachedOnly){
    const body=batch.map(({pane,terms})=>({pane,terms}));
    const response=await fetch('/api/search/pane-content'+(cachedOnly?'?cached_only=1':''),{method:'POST',
      signal:run.controller.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(response.status===401){location='/login';throw new Error('登录已失效');}
    if(!response.ok)throw new Error('HTTP '+response.status);
    return (await response.json()).results;
  }
  function applySearchResult(run,hit){
    if(hit.snippet)contentHits.set(hit.pane,hit);
    else if(!hit.unavailable)contentHits.delete(hit.pane);
    if(hit.unavailable)run.unread++;
    for(const route of hit.routes||[])run.routes[route]=(run.routes[route]||0)+1;
    run.done++;
  }
  async function searchContent(run,remaining){
    const queues=new Map();
    function enqueue(item,route){
      if(!queues.has(route))queues.set(route,[]);
      queues.get(route).push(item);
    }
    try{
      run.stage='检查服务端缓存';paintSearchProgress(run);
      for(let i=0;i<remaining.length;i+=32){
        if(run!==contentRun)return;
        const batch=remaining.slice(i,i+32);
        try{
          const hits=await fetchSearchBatch(batch,run,true);
          if(run!==contentRun)return;
          const byPane=new Map(hits.map(hit=>[hit.pane,hit]));
          for(const item of batch){
            const hit=byPane.get(item.pane);
            if(!hit||hit.next)enqueue(item,hit?.next||'online');
            else applySearchResult(run,hit);
          }
        }catch(error){
          if(run!==contentRun)return;
          for(const item of batch)enqueue(item,'online');
        }
        renderList();paintSearchProgress(run);
      }
      for(const route of ['local','agent','federation','online','ssh']){
        const items=queues.get(route)||[];
        for(let i=0;i<items.length;i+=8){
          if(run!==contentRun)return;
          const batch=items.slice(i,i+8);
          const hosts=[...new Set(batch.map(item=>item.host))];
          run.stage='正在查询 '+searchRoutes[route]+' · '+hosts[0]+(hosts.length>1?' 等 '+hosts.length+' 台':'');
          paintSearchProgress(run);
          try{
            const hits=await fetchSearchBatch(batch,run,false);
            if(run!==contentRun)return;
            const byPane=new Map(hits.map(hit=>[hit.pane,hit]));
            for(const item of batch)applySearchResult(run,byPane.get(item.pane)||{pane:item.pane,unavailable:true});
          }catch(error){
            if(run!==contentRun)return;
            run.unread+=batch.length;run.done+=batch.length;
          }
          renderList();paintSearchProgress(run);
        }
      }
    }finally{
      if(run===contentRun){
        clearInterval(run.clock);run.pending=false;run.stage='搜索完成';renderList();paintSearchProgress(run);
      }
    }
  }
  async function searchBrowser(run,candidates){
    const remaining=[];
    for(let i=0;i<candidates.length;i++){
      if(run!==contentRun)return;
      const item=candidates[i],hit=browserMatch(item);
      if(hit){contentHits.set(item.pane,hit);run.browser++;}
      if(hit?.fresh)run.done++;
      else remaining.push(item);
      if(i%8===7)await new Promise(resolve=>setTimeout(resolve,0));
    }
    if(run!==contentRun)return;
    renderList();paintSearchProgress(run);
    if(!remaining.length){await searchContent(run,remaining);return;}
    contentTimer=setTimeout(()=>searchContent(run,remaining),Math.max(0,450-(performance.now()-run.started)));
  }
  function updatePaneSearch(query){
    clearTimeout(contentTimer);
    if(contentRun){contentRun.controller.abort();clearInterval(contentRun.clock);}
    paneSearchQuery=query;
    contentHits.clear();
    const terms=parsePaneSearchTerms(query);
    const candidates=terms.length?searchCandidates(terms):[];
    contentRun=terms.length?{controller:new AbortController(),pending:true,unread:0,tree:searchTreeKey(),
      started:performance.now(),stage:'检查浏览器缓存',total:candidates.length,done:0,routes:{},browser:0}:null;
    renderList();paintSearchProgress(contentRun);
    const run=contentRun;
    if(run){
      run.clock=setInterval(()=>paintSearchProgress(run),1000);
      contentTimer=setTimeout(()=>searchBrowser(run,candidates),65);
    }
  }
  function searchTreeKey(){
    return state.panes.map(p=>p.id+'@'+(p.pane_pid||'')).sort().join('|');
  }
  function renderHostList(){
    if(contentRun&&contentRun.tree!==searchTreeKey())updatePaneSearch(paneSearchQuery);
    else renderList();
  }
  function clearPaneSearch(){const field=$('pane-search');if(field)field.value='';updatePaneSearch('');}
  $('pane-search').addEventListener('input',event=>{if(!event.isComposing)updatePaneSearch(event.target.value);});
  $('pane-search').addEventListener('compositionend',event=>updatePaneSearch(event.target.value));
  $('pane-search').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();clearPaneSearch();}});
  $('grid-add').onclick=()=>{if(!state.selected){showError('先选择一个 pane');return;}location='/grid?add='+encodeURIComponent(state.selected);};$('session-form').onsubmit=e=>{e.preventDefault();submitSessionForm();};$('session-cancel').onclick=()=>$('session-dialog').close();$('session-agent').onchange=()=>{const on=!!$('session-agent').value;$('session-args-field').hidden=!on;syncAgentHint();if(on)$('session-args').focus();};
  mountComposerFiles();
  $('live-toggle').onclick=()=>{state.livePaused?resumeLive():pauseLive();};
  // 「暂停实时刷新」是实验性功能：默认藏起来，设置里打开才显示
  function applyPauseButton(on){
    const button=$('live-toggle');if(!button)return;
    button.hidden=!on;
    if(!on&&state.livePaused)resumeLive();
  }
  try{applyPauseButton(localStorage.getItem('tmux-pause-button')==='on');}catch{applyPauseButton(false);}
  window.addEventListener('tmuxweb:pause-button',event=>applyPauseButton(!!event.detail));$('fav-toggle').onclick=()=>{if(state.selected)toggleFavorite(state.selected);};
  $('model-toggle').onclick=()=>{const open=$('model-menu').hidden;openModelMenu(open);if(open)loadPaneModels(state.selected);};$('model-filter').oninput=event=>filterModels(event.target.value);document.addEventListener('click',event=>{if($('model-menu').hidden)return;const wrap=$('model-pick-wrap');if(!wrap.contains(event.target))openModelMenu(false);});document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('model-menu').hidden)openModelMenu(false);});$('route-switch').onclick=cycleRoute;paintRoute(currentRoute());window.addEventListener('tmux-route-change',()=>paintRoute(currentRoute()));$('pane-close').onclick=()=>openPaneCloseDialog();$('pane-close-cancel').onclick=()=>$('pane-close-dialog').close();$('pane-close-confirm').onclick=()=>closeSelectedPane();$('refresh').onclick=()=>{if(state.selected&&state.mode==='chat')refreshTranscript(state.selected,{force:true,refresh:true});loadPanes(true);};$('menu').onclick=openSidebar;$('sidebar-close').onclick=closeSidebar;$('drawer-backdrop').onclick=closeSidebar;$('chat-mode').onclick=()=>setMode('chat');$('terminal-mode-button').onclick=()=>setMode('terminal');$('fullscreen').onclick=toggleFullscreen;$('share-button').onclick=()=>{$('share-result').textContent='';$('share-slug').value=`share-${Date.now().toString(36)}`;$('share-allow-input').checked=true;$('share-allow-model').checked=false;$('share-allow-model').disabled=!(state.panes.find(p=>p.id===state.selected)?.provider||isAgentPane(state.selected));$('share-allow-terminal').checked=false;$('share-dialog').showModal();};$('share-cancel').onclick=()=>$('share-dialog').close();$('share-form').onsubmit=createShare;document.addEventListener('fullscreenchange',syncFullscreen);document.addEventListener('visibilitychange',()=>{clearTimeout(state.hiddenTimer);if(document.hidden&&state.mode==='terminal')state.hiddenTimer=setTimeout(()=>{if(document.hidden&&state.mode==='terminal')setMode('chat');},15000);});window.addEventListener('beforeunload',closeConnections);document.addEventListener('keydown',e=>{if(e.key==='Escape'){if($('sidebar').classList.contains('open'))closeSidebar();else if($('main').classList.contains('fullscreen-fallback'))toggleFullscreen();}});send.onclick=()=>{if(state.mode==='chat'&&state.selected&&agentWorking()&&!input.value.trim()){stopAgent();return;}sendInput();};input.addEventListener('focus',()=>setComposing(true));input.addEventListener('blur',()=>setTimeout(()=>setComposing(keyboardWanted()),120));input.oninput=()=>{syncSendButton();input.style.height='auto';input.style.height=Math.min(input.scrollHeight,82)+'px';if(state.selected)state.drafts.set(state.selected,input.value);refreshInputComplete(true);};input.onkeydown=e=>{
    // 补全弹窗打开时，方向键/Enter/Tab/Esc 归补全；Esc 只关弹窗，不停 agent。
    if(!completeOpen && completeMatch && e.key==='Escape'){e.preventDefault();completeSuppressed=true;closeInputComplete();return;}
    if(completeOpen){
      if(e.key==='Escape'){e.preventDefault();completeSuppressed=true;closeInputComplete();return;}
      if(e.key==='ArrowDown'){e.preventDefault();completeMove(1);return;}
      if(e.key==='ArrowUp'){e.preventDefault();completeMove(-1);return;}
      if((e.key==='Enter'&&!e.shiftKey&&!e.isComposing)||e.key==='Tab'){e.preventDefault();acceptInputComplete(completeItems[completeIndex]);return;}
    }
    if(e.key==='Escape'&&state.mode==='chat'&&state.selected&&agentWorking()){e.preventDefault();stopAgent();return;}
    if(e.key!=='Enter'||e.shiftKey||e.isComposing||isMobile())return;e.preventDefault();sendInput();};
  // 光标移动（方向键/鼠标点选）也可能移进或移出一个 @ / / 词：跟着刷新；
  // mousedown 抢在 blur 之前接住点选，否则输入框一失焦弹窗就没了。
  input.addEventListener('keyup',()=>refreshInputComplete(false));
  input.addEventListener('click',()=>refreshInputComplete(false));
  input.addEventListener('blur',()=>closeInputComplete());
  document.addEventListener('click',event=>{if(!completeOpen)return;const box=$('input-complete');if(box&&!box.contains(event.target)&&event.target!==input)closeInputComplete();});
  {const box=$('input-complete');
  if(box){box.addEventListener('mousedown',event=>{const item=event.target.closest('[data-complete-index]');if(!item)return;event.preventDefault();acceptInputComplete(completeItems[Number(item.dataset.completeIndex)]);});
  box.addEventListener('mousemove',event=>{const item=event.target.closest('[data-complete-index]');if(!item)return;const next=Number(item.dataset.completeIndex);box.dataset.navigated='1';if(next!==completeIndex){completeIndex=next;paintCompleteSelection();}});}}scroll.addEventListener('wheel',()=>{state.stick=false;$('jump').style.display='block';},{passive:true});scroll.addEventListener('touchmove',()=>{state.stick=false;$('jump').style.display='block';},{passive:true});scroll.onscroll=()=>{state.stick=atBottom();
    if(scroll.scrollTop<32&&state.hasMore&&!state.loadingEarlier&&state.transcriptLimit<TRANSCRIPT_PAGE_MAX)loadEarlier();
    $('jump').style.display=state.stick?'none':'block';};$('jump').onclick=()=>{state.stick=true;scroll.scrollTop=scroll.scrollHeight;$('jump').style.display='none';};window.addEventListener('resize',()=>{handleVisualViewportChange();if(!isMobile()){$('sidebar').classList.remove('open');$('drawer-backdrop').classList.remove('show');document.querySelector('.app').classList.remove('mobile-composing');}fitCapture();scheduleTerminalFit();});if(window.visualViewport){visualViewport.addEventListener('resize',handleVisualViewportChange);visualViewport.addEventListener('scroll',handleVisualViewportChange);}useEditableCapture();setupTerminalControls();setupTerminalInput();setupFiles();setupGitPanel();setupQueueStrip();setupPanePreview();// 侧栏每次进控制台都从会话列表开始：文件面板是"我要看文件"这个动作的结果，不是一个
// 会跨页面加载记住的偏好。以前这里会读 localStorage，于是你上次开着文件面板，之后每次
// 进 chat（主页卡片、多窗口、直接链接）侧栏都先弹文件管理，看起来像是随机的。
try{localStorage.removeItem('tmux-files-mode');}catch{}syncVisualViewport();if(localStorage.getItem('tmux-sidebar-collapsed')==='1'&&!isMobile())document.querySelector('.app').classList.add('sidebar-collapsed');
  (function landOnBoard(){
    const initialParams=new URLSearchParams(location.search);
    const requestedMode=initialParams.get('mode');
    const initialMode=['chat','terminal'].includes(requestedMode)?requestedMode:localStorage.getItem('tmux-web-mode');
    if(initialMode==='terminal')setMode('terminal');
    const wanted=(initialParams.get('pane')||localStorage.getItem('tmux-last-pane')||'').trim();
    state.wantedPane=wanted||null;
    if(!wanted&&!initialParams.has('inbox')&&!initialParams.has('settings'))location.replace('/home');
  })();
  // 「最近」块现在是三个页面共用的一份实现（assets/recents.js）
  if(window.TmuxRecents){
    TmuxRecents.mount({
      onPick: pane => { selectPane(pane); if(isMobile())closeSidebar(); },
      hideWhenFiles: true,
      // 名字先到的是「最近/收藏」，pane 标题后到：任一边更新都重画一次顶栏
      onNames: () => {
        const id = state.selected;
        if (!id) return;
        const p = state.panes.find(x => x.id === id);
        if (!p) return;
        const shown = (TmuxRecents.nameFor ? TmuxRecents.nameFor(id) : '') || p.title || p.target;
        $('selected-title').textContent = shown;
        document.title = p.number ? `${paneLabel(p)} · ${shown} · Tmux Chat` : 'Tmux Chat';
      },
    });
    TmuxRecents.setHosts(state.hosts);
    setupRecentPreview();
  }
  // 从「服务器管理」按返回设置回来：?settings=1 直接把设置打开，参数从地址里抹掉
  (function openSettingsFromQuery(){
    let wanted=false;
    try{wanted=new URLSearchParams(location.search).get('settings')==='1';}catch{}
    if(!wanted)return;
    replaceConsoleQuery(params=>params.delete('settings'));
    const attempt=(tries)=>{
      if(window.TmuxSettings&&TmuxSettings.open){TmuxSettings.open();return;}
      if(tries>0)setTimeout(()=>attempt(tries-1),120);
    };
    attempt(20);
  })();
  paintCachedHosts();state.serverVersion=serverVersionBaseline();loadPanes();setInterval(()=>{if(!document.hidden){loadPanes();state.versionPollRound++;if(shouldCheckVersion(state.versionPollRound,VERSION_CHECK_EVERY_ROUNDS))checkServerVersion();}},20000);
  if(window.TmuxPrompt)TmuxPrompt.start(()=>state.mode==='chat'?state.selected:null,
    currentInteraction, toast);
  $('inspect-interaction').onclick=()=>{
    if($('inspect-interaction').hidden)return;
    closeChatReply();
    window.TmuxPrompt?.inspect(()=>state.mode==='chat'?state.selected:null);
  };
  setInterval(()=>{if(!document.hidden){syncNativeForm(true);refreshPaneStates();}},9000);
  // 子 agent 调用图：只在抽屉打开时轮询
  if(window.TmuxAgents)TmuxAgents.mount({getPane:()=>state.selected});
  if(window.TmuxChatSearch)TmuxChatSearch.mount();
  // 等你回答收件箱（借鉴清单第 7 项）：亮灯历史 + 未读数，点记录跳 pane。
  if(window.TmuxNeedsYouInbox)TmuxNeedsYouInbox.start({onJump:pane=>selectPane(pane)});
  // 从主页按铃铛过来：?inbox=1 直接把收件箱抽屉打开，参数从地址里抹掉
  (function openInboxFromQuery(){
    let wanted=false;
    try{wanted=new URLSearchParams(location.search).get('inbox')==='1';}catch{}
    if(!wanted)return;
    replaceConsoleQuery(params=>params.delete('inbox'));
    const attempt=(tries)=>{
      if(window.TmuxNeedsYouInbox&&TmuxNeedsYouInbox.open){TmuxNeedsYouInbox.open();return;}
      if(tries>0)setTimeout(()=>attempt(tries-1),120);
    };
    attempt(20);
  })();
  fetch('/api/prefs',{credentials:'same-origin'}).then(r=>r.json()).then(data=>{
    const pauseValue=(data.prefs||{}).pause_button;
    if(pauseValue==='on'||pauseValue==='off'){
      try{localStorage.setItem('tmux-pause-button',pauseValue);}catch{}
      applyPauseButton(pauseValue==='on');
    }
  }).catch(()=>{});

  if('serviceWorker' in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}));
})();
(function initSidebarResize(){
  const KEY = 'tmux-side-w', DEFAULT = 280, MIN = 200;
  const handle = document.getElementById('side-resizer');
  const bar = document.getElementById('sidebar');
  if(!handle || !bar) return;
  const root = document.documentElement;
  const clamp = width => Math.max(MIN, Math.min(Math.min(640, Math.round(window.innerWidth * 0.6)), Math.round(width)));
  const apply = (width, save) => {
    const value = clamp(width);
    root.style.setProperty('--side-w', value + 'px');
    if(save){try{localStorage.setItem(KEY, String(value));}catch{}}
  };
  let saved = 0;
  try{saved = parseInt(localStorage.getItem(KEY) || '', 10);}catch{}
  if(saved) apply(saved, false);
  let dragging = false, startX = 0, startWidth = 0;
  handle.addEventListener('pointerdown', event => {
    if(event.button !== 0) return;
    event.preventDefault();
    dragging = true;
    startX = event.clientX;
    startWidth = bar.getBoundingClientRect().width;
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing');
  });
  handle.addEventListener('pointermove', event => {
    if(dragging) apply(startWidth + (event.clientX - startX), false);
  });
  const finish = event => {
    if(!dragging) return;
    dragging = false;
    if(event && event.pointerId != null && handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove('resizing');
    apply(bar.getBoundingClientRect().width, true);
  };
  handle.addEventListener('pointerup', finish);
  handle.addEventListener('pointercancel', finish);
  handle.addEventListener('dblclick', () => apply(DEFAULT, true));
  window.addEventListener('resize', () => apply(bar.getBoundingClientRect().width, false));
})();

