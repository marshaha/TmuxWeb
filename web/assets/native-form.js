/* A provider-owned request is the only source of fields and choices. */
(() => {
  const box = document.getElementById('native-form');
  if (!box) return;
  const form = box.querySelector('form'), fields = box.querySelector('.native-fields');
  const title = box.querySelector('h3'), note = box.querySelector('[role=status]');
  const send = form.querySelector('button');
  let pane = '', current = null, notice = '', epoch = 0, reading = false, busy = false;
  let provider = 'Hermes';
  let completed = '', draft = {}, lastRead = 0;
  function draftKey() { return 'tw.form.' + pane; }
  function remember() {
    try { sessionStorage.setItem(draftKey(), JSON.stringify({revision:current.revision, draft})); } catch {}
  }
  function markOpen(revision) {
    if ((box.dataset.open || '') === revision) return;
    if (revision) box.dataset.open = revision;
    else delete box.dataset.open;
    box.dispatchEvent(new Event('native-form-change'));
  }
  function status(message) {
    form.hidden = true;
    title.textContent = provider + ' 交互';
    note.textContent = message;
    box.hidden = false;
    markOpen('');
  }
  function render(next) {
    completed = '';
    current = next;
    provider = next.provider || 'Hermes';
    try {
      const saved = JSON.parse(sessionStorage.getItem(draftKey()));
      draft = saved?.revision === next.revision ? saved.draft || {} : {};
    } catch { draft = {}; }
    fields.replaceChildren();
    title.textContent = next.kind === 'approval' ? 'Hermes 请求审批' : provider + ' 等你回答';
    for (const [position, question] of next.questions.entries()) {
      const group = document.createElement('fieldset'), legend = document.createElement('legend');
      legend.textContent = question.title;
      group.append(legend);
      if (question.description) {
        const description = document.createElement('p');
        description.textContent = question.description;
        group.append(description);
      }
      const controls = [];
      const saved = draft[question.id] || {};
      const otherChoice = question.other && question.choices.length > 0;
      let otherInput;
      function read() {
        const indices = [];
        controls.forEach((c,i) => { if (c.checked && i < question.choices.length) indices.push(i); });
        const other = question.other && (!otherChoice || controls.at(-1).checked);
        if (otherInput) {
          otherInput.hidden = !other;
          otherInput.required = !!other;
        }
        draft[question.id] = other ? {indices, other:otherInput.value} : {indices};
      }
      for (const [i, text] of [...question.choices, ...(otherChoice ? ['其他（自己填写）'] : [])].entries()) {
        const label = document.createElement('label'), input = document.createElement('input');
        input.type = question.multi ? 'checkbox' : 'radio';
        input.name = 'native-question-' + position;
        input.checked = i < question.choices.length ? (saved.indices || []).includes(i) : typeof saved.other === 'string';
        input.addEventListener('change', () => { read(); remember(); });
        label.append(input, document.createTextNode(text));
        group.append(label);
        controls.push(input);
      }
      if (question.other) {
        otherInput = document.createElement('textarea');
        otherInput.rows = 2;
        otherInput.maxLength = 2000;
        otherInput.setAttribute('aria-label',question.title + '（填写回答）');
        otherInput.placeholder = '输入你的回答';
        otherInput.value = saved.other || '';
        otherInput.addEventListener('input', () => { read(); remember(); });
        group.append(otherInput);
      }
      read();
      fields.append(group);
    }
    send.textContent = next.kind === 'approval' ? '提交审批决定' : '提交全部答案';
    send.disabled = false;
    form.hidden = false;
    box.hidden = false;
    note.textContent = provider === 'Codex' ? '可先改选。提交后，答案会作为聊天消息发回本轮对话。' : '可修改选项，确认后提交';
    markOpen(next.revision);
  }
  async function call(method, body) {
    const res = await fetch('/api/panes/'+encodeURIComponent(pane)+'/native-form', {
      method, credentials:'same-origin', cache:'no-store',
      ...(body ? {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})
    });
    if (!res.ok) throw new Error('无法读取原生请求');
    return res.json();
  }
  form.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.matches('input')) event.preventDefault();
  });
  form.onsubmit = async event => {
    event.preventDefault();
    if (event.submitter !== send || busy || !current) return;
    if (current.questions.some(q => !draft[q.id]?.indices.length && !draft[q.id]?.other?.trim())) {
      note.textContent = '请完成每道题再提交';
      return;
    }
    const version = ++epoch, revision = current.revision;
    reading = false;
    busy = true;
    send.disabled = true;
    try {
      const result = await call('POST', {revision, answers:draft});
      if (version !== epoch) return;
      if (result.status === 'resolved' || result.status === 'closed') {
        completed = revision;
        current = null;
        try { sessionStorage.removeItem(draftKey()); } catch {}
        status(result.status === 'resolved' ? (provider === 'Codex' ? '回答已发送到本轮 Codex 对话' : '已回答') : '本题已结束或已由其他设备处理');
      } else {
        note.textContent = result.status === 'invalid' ? '请检查各题的选择' : '还未确认结果，请查看实时终端';
      }
    } catch { if (version === epoch) note.textContent = '连接中断，提交结果待核对'; }
    finally { if (version === epoch) { busy = false; send.disabled = false; } }
  };
  async function sync(id, signal, poll = false, probe = false) {
    const first = id !== pane;
    const key = signal?.revision || '';
    if (first) {
      ++epoch;
      provider = signal?.source === 'codex_followup' ? 'Codex' : 'Hermes';
      pane = id; notice = key; current = null; completed = ''; reading = false; busy = false;
      box.hidden = true;
      markOpen('');
      poll = true;
    }
    const newNotice = key !== notice;
    notice = key;
    if (!id || !poll || busy || document.hidden || reading) return;
    const interval = current || signal || probe ? 3000 : 8000;
    if (!first && !newNotice && Date.now()-lastRead < interval) return;
    lastRead = Date.now();
    const version = epoch;
    reading = true;
    try {
      const result = await call('GET');
      if (version !== epoch || busy) return;
      if (result.status === 'open' && result.form) {
        if (completed !== result.form.revision && current?.revision !== result.form.revision) render(result.form);
      } else if (!completed) {
        const wasOpen = !!current;
        current = null;
        if (wasOpen || signal) status(wasOpen && result.status === 'closed' ? '本题已结束或已由其他设备处理' : '请在实时终端作答');
        else box.hidden = true;
      }
    } catch {
      if (version === epoch && !busy && !completed) { if (current || signal) status('暂时无法核对题目，请查看实时终端'); current = null; }
    } finally { if (version === epoch) reading = false; }
  }
  window.TmuxNativeForm = {sync};
})();
