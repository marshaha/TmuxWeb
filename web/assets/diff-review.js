/* The diff is text. Only the selected hunk needs interactive controls. */
(() => {
  const $ = id => document.getElementById(id);
  function hunks(patch, requestedPath) {
    const list = [];
    let file = requestedPath, prior = '', current = null;
    for (const line of patch.split('\n')) {
      if (line.startsWith('diff --git ')) { current = null; file = requestedPath; prior = ''; }
      else if (!current && line.startsWith('--- ')) prior = line.slice(4).split('\t')[0].replace(/^a\//,'');
      else if (!current && line.startsWith('+++ ')) {
        const next = line.slice(4).split('\t')[0];
        file = requestedPath || (next === '/dev/null' ? prior : next.replace(/^b\//,''));
      } else if (/^@@ -\d+(,\d+)? \+\d+(,\d+)? @@/.test(line)) {
        current = {file, header:line, lines:[]};
        list.push(current);
      } else if (current) current.lines.push(line);
    }
    return list;
  }

  function mount({api, pane, targets, label}) {
    const section = $('gp-review'), pick = $('review-hunk'), text = $('review-note'),
      list = $('review-list'), send = $('review-send'), target = $('review-target'), status = $('review-status');
    const drafts = new Map();
    let shown, busy = false, offset = 0;
    function notes() {
      if (!drafts.has(shown.pane)) drafts.set(shown.pane, []);
      return drafts.get(shown.pane);
    }
    function draw() {
      const items = notes();
      list.replaceChildren();
      for (const note of items) {
        const row = document.createElement('li'), remove = document.createElement('button');
        remove.type = 'button'; remove.textContent = '删除';
        remove.disabled = busy;
        remove.onclick = () => { items.splice(items.indexOf(note),1); draw(); };
        row.append(document.createTextNode(note.file+' '+note.header+' · '+note.comment+' '),remove);
        list.append(row);
      }
      send.disabled = busy || !items.length;
      $('review-add').disabled = busy || !shown.hunks.length;
    }
    function excerpt() {
      const h = shown.hunks[offset+pick.selectedIndex];
      if (!h) return '';
      const snippet = h.lines.slice(0,35).join('\n').slice(0,2400);
      return snippet;
    }
    const linePick = $('review-line');
    function lineAnchor(h, i) {
      const coords = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(h.header);
      let oldLine = +coords[1], newLine = +coords[2];
      for (const previous of h.lines.slice(0,i)) {
        if (/^[ -]/.test(previous)) oldLine++;
        if (/^[ +]/.test(previous)) newLine++;
      }
      return h.lines[i].startsWith('-') ? '旧文件 L'+oldLine : '新文件 L'+newLine;
    }
    function showLines() {
      const h = shown?.hunks[offset+pick.selectedIndex];
      const selected = Number(linePick.value) || 0;
      const from = Math.floor(Math.max(0,selected-1)/35)*35;
      const excerpt = $('review-excerpt'); excerpt.replaceChildren();
      (h?.lines || []).slice(from,from+35).forEach((line,i)=>{
        const button=document.createElement('button'), n=from+i+1;
        button.type='button'; button.textContent=n+'  '+line.slice(0,320);
        button.setAttribute('aria-pressed',String(n===selected));
        button.onclick=()=>{linePick.value=n; showLines(); text.focus();};
        excerpt.append(button);
      });
    }
    linePick.oninput=showLines;
    pick.onchange=()=>{
      linePick.value=''; linePick.max=shown.hunks[offset+pick.selectedIndex]?.lines.length || 0; showLines();
    };
    function page(start) {
      offset=start;
      pick.replaceChildren(...shown.hunks.slice(start,start+64).map(h=>new Option(h.file+' '+h.header.slice(0,80))));
      $('review-pages').hidden=shown.hunks.length<=64;
      $('review-prev').disabled=start===0; $('review-next').disabled=start+64>=shown.hunks.length;
      pick.onchange();
    }
    $('review-prev').onclick=()=>page(Math.max(0,offset-64));
    $('review-next').onclick=()=>page(offset+64);
    $('review-add').onclick = () => {
      const h = shown?.hunks[offset+pick.selectedIndex], comment = text.value.trim();
      if (!h || !comment || busy || pane() !== shown.pane) return;
      if (notes().length >= 12) { status.textContent = '每批最多 12 条意见'; return; }
      const line = linePick.value === '' ? 0 : Number(linePick.value);
      if (!Number.isInteger(line) || line < 0 || line > h.lines.length || linePick.validity.badInput) {
        status.textContent = '请输入本段范围内的序号'; return;
      }
      const context = line ? h.lines[line-1] : excerpt();
      const anchor = line ? h.header+' · '+lineAnchor(h,line-1) : h.header;
      notes().push({file:h.file, header:anchor, excerpt:context, comment,
                    revision:shown.revision, url:shown.url});
      text.value = ''; status.textContent = ''; draw();
    };
    send.onclick = async () => {
      const view = shown, recipient = target.value;
      if (busy || !view || pane() !== view.pane || !recipient || !notes().length) return;
      busy = true; draw(); target.disabled = true;
      const batch = notes().slice();
      let submitting = false;
      try {
        const revisions = new Map();
        for (const n of batch) {
          if (!revisions.has(n.url)) revisions.set(n.url,(await api(n.url)).revision);
          if (revisions.get(n.url) !== n.revision) throw new Error(n.file+' 的 diff 已变化，请删除旧批注并重新选择');
        }
        if (view !== shown || pane() !== view.pane) return;
        const message = '代码审查意见\n'+batch.map((n,i) =>
          (i+1)+'. '+n.file+' '+n.header+' [diff '+n.revision.slice(0,12)+']\n'+n.excerpt+'\n意见：'+n.comment
        ).join('\n\n');
        if (message.length > 20000) throw new Error('这批意见超过两万字，请分批发送');
        submitting = true;
        const response = await api('/api/relay/message',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({caller:view.pane,target:recipient,text:message,submit:true})});
        drafts.get(view.pane).splice(0,batch.length);
        if (view === shown) status.textContent = response.submitted ? '已提交到 '+label(recipient) : '已粘贴，等待提交确认';
      } catch (err) {
        if (view === shown) status.textContent = submitting ? '提交结果待核对，请先查看发送回执：'+err.message : err.message;
      } finally { busy = false; target.disabled = false; if (shown) draw(); }
    };
    return {
      hide() { shown = null; section.hidden = true; text.value = ''; },
      show(id,url,path,data) {
        shown = {pane:id,url,revision:data.revision,hunks:hunks(data.patch||'',path)};
        section.hidden = !data.revision || (!shown.hunks.length && !notes().length);
        page(0);
        target.replaceChildren(...targets().map(p => new Option(label(p.id),p.id)));
        target.value = id;
        status.textContent = ''; text.value = ''; draw();
      }
    };
  }
  window.TmuxDiffReview = {mount};
})();
