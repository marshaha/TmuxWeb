/* 性能预算：掉帧算 bug，先用数字说话。
 *
 * 打开方式：`?perf=1`（或 localStorage['tmux-perf']='1'）。左上角出现一个小方块，
 * 滚动时实时显示帧时间 p95 / 最长任务 / 节点数；每 15 秒把一行结果 POST 到
 * /api/client-trace，写进服务日志 —— 手机上复现卡顿时不用一直盯着屏幕。
 *
 *   TmuxPerf.start()                 开始采样 + 显示
 *   TmuxPerf.sample({ms: 3000})      返回 {fps, frameP95, longestTask, nodes, verdict}
 *   TmuxPerf.budget                  预算常量
 *
 * 预算的含义（超了就当成 bug 修，不是"手感问题"）：
 *   frameP95   <= 24ms   60Hz 下掉一帧以内
 *   longestTask<= 120ms  一次长任务不至于让点击明显迟滞
 *   nodes      <= 12000  500 条消息封顶时的 DOM 规模上限
 */
(function () {
  const BUDGET = {frameP95: 24, longestTask: 120, nodes: 12000};
  const REPORT_MS = 15000;

  let running = false;
  let frames = [];
  let longestTask = 0;
  let taskCount = 0;
  let rafId = 0;
  let last = 0;
  let reportTimer = 0;
  let box = null;

  const $ = id => document.getElementById(id);

  function ensureBox() {
    if (box) return box;
    box = document.createElement('div');
    box.id = 'perf-box';
    box.style.cssText = 'position:fixed;left:10px;top:10px;z-index:99999;padding:6px 8px;border-radius:8px;'
      + 'font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(15,15,14,.86);'
      + 'color:#e8e6e1;border:1px solid rgba(255,255,255,.18);pointer-events:none;white-space:pre';
    box.textContent = 'perf: 采样中…';
    document.body.appendChild(box);
    return box;
  }

  function frame(now) {
    if (!running) return;
    if (last) frames.push(now - last);
    last = now;
    if (frames.length > 240) frames.shift();
    rafId = requestAnimationFrame(frame);
  }

  function observeTasks() {
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) {
          taskCount += 1;
          longestTask = Math.max(longestTask, entry.duration);
        }
      }).observe({entryTypes: ['longtask']});
    } catch (_error) { /* Safari 没有 longtask，就只看帧 */ }
  }

  function percentile(values, p) {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
  }

  function stats() {
    const p95 = percentile(frames, 0.95);
    const mean = frames.length ? frames.reduce((a, b) => a + b, 0) / frames.length : 0;
    const nodes = document.getElementsByTagName('*').length;
    const messages = document.querySelectorAll('.chat-message').length;
    return {
      fps: mean ? Math.round(1000 / mean) : 0,
      frameP95: Math.round(p95 * 10) / 10,
      longestTask: Math.round(longestTask),
      tasks: taskCount,
      nodes,
      messages,
    };
  }

  function verdict(s) {
    return s.frameP95 <= BUDGET.frameP95 && s.longestTask <= BUDGET.longestTask
      && s.nodes <= BUDGET.nodes;
  }

  function paint() {
    const s = stats();
    const ok = verdict(s);
    ensureBox().textContent = `${ok ? '✓' : '✗'} ${s.fps}fps · p95 ${s.frameP95}ms · `
      + `长任务 ${s.longestTask}ms×${s.tasks} · ${s.messages} 条 / ${s.nodes} 节点`;
    box.style.borderColor = ok ? 'rgba(63,185,80,.7)' : 'rgba(248,81,73,.8)';
    return s;
  }

  function report() {
    const s = stats();
    s.verdict = verdict(s);
    try {
      fetch('/api/client-trace', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          pane: (new URLSearchParams(location.search).get('pane') || ''),
          last_id: '', rendered: s.messages,
          note: `perf fps=${s.fps} p95=${s.frameP95} task=${s.longestTask} nodes=${s.nodes}`
            + ` ok=${s.verdict ? 1 : 0}`,
        }),
      }).catch(() => {});
    } catch (_error) { /* 采样上报失败不影响页面 */ }
  }

  function start() {
    if (running) return;
    running = true;
    frames = [];
    longestTask = 0;
    taskCount = 0;
    last = 0;
    observeTasks();
    rafId = requestAnimationFrame(frame);
    reportTimer = setInterval(() => { paint(); report(); }, REPORT_MS);
    paint();
  }

  function stop() {
    running = false;
    cancelAnimationFrame(rafId);
    clearInterval(reportTimer);
    if (box) box.remove();
    box = null;
  }

  /** 采样一段时间（默认 3 秒），可选地驱动滚动：卡顿只有动起来才看得见。 */
  function sample(options) {
    const ms = (options && options.ms) || 3000;
    const scroll = !options || options.scroll !== false;
    if (!running) start();
    const target = document.getElementById('transcript') || document.scrollingElement;
    const startAt = performance.now();
    let direction = -1;
    if (scroll) {
      target.scrollTop = target.scrollHeight;
    }
    const ticker = setInterval(() => {
      if (!scroll) return;
      const max = Math.max(0, target.scrollHeight - target.clientHeight);
      target.scrollTop = Math.max(0, Math.min(max, target.scrollTop + direction * 160));
      if (target.scrollTop <= 0) direction = 1;
      if (target.scrollTop >= max) direction = -1;
    }, 60);
    return new Promise(resolve => {
      setTimeout(() => {
        clearInterval(ticker);
        const s = paint();
        s.verdict = verdict(s);
        s.windowMs = Math.round(performance.now() - startAt);
        resolve(s);
      }, ms);
    });
  }

  window.TmuxPerf = {start, stop, sample, stats, paint, report, budget: BUDGET};
})();
