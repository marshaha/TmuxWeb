/* 本地缓存：让"打开一个 pane"和"从服务器管理返回"不必先白等一轮网络。
 *
 * 只存"最后看到的样子"，永远不当作真值：读出来先画上去（标成缓存），
 * 后面照旧走网络覆盖。所以缓存坏了/过期了，最多是闪一下旧内容，
 * 不会让界面停在错误状态。
 *
 *   TmuxPaneCache.saveHosts(hosts)         / readHosts(maxAgeMs)
 *   TmuxPaneCache.saveTranscript(id, data) / readTranscript(id, maxAgeMs)
 *   TmuxPaneCache.clear()
 *
 * 体积上限硬顶住 localStorage 的 5MB：单条 transcript 只留最后 40 条消息，
 * 总量超了按最久没用的先删。
 */
(function () {
  const HOSTS_KEY = 'tmuxweb.cache.hosts.v1';
  const TX_PREFIX = 'tmuxweb.cache.tx.';
  const MAX_TX = 12;
  const PER_TX_CHARS = 400_000;
  const TOTAL_CHARS = 3_000_000;
  const TX_MESSAGES = 40;

  function readJSON(key) {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_error) { return null; }
  }

  function writeJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (_error) {
      prune(0);                       // 空间不够：把缓存让出来再试一次
      try {
        localStorage.setItem(key, JSON.stringify(value));
        return true;
      } catch (_error) { return false; }
    }
  }

  function txKeys() {
    const keys = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key && key.startsWith(TX_PREFIX)) keys.push(key);
    }
    return keys;
  }

  // 按体积 + 条数收一遍；keepKey 那条不动
  function prune(keepCount, keepKey) {
    const entries = txKeys().map(key => {
      const value = readJSON(key) || {};
      return {key, at: value.at || 0, size: (localStorage.getItem(key) || '').length};
    }).sort((left, right) => left.at - right.at);

    let total = entries.reduce((sum, item) => sum + item.size, 0);
    while (entries.length > Math.max(0, keepCount)) {
      const oldest = entries.shift();
      if (!oldest) break;
      if (oldest.key === keepKey) continue;
      localStorage.removeItem(oldest.key);
      total -= oldest.size;
    }
    while (total > TOTAL_CHARS && entries.length) {
      const oldest = entries.shift();
      if (oldest.key === keepKey) continue;
      localStorage.removeItem(oldest.key);
      total -= oldest.size;
    }
  }

  // ---------------------------------------------------------------- hosts
  function saveHosts(hosts) {
    const compact = (hosts || []).map(host => ({
      id: host.id,
      name: host.name,
      kind: host.kind,
      prefix: host.prefix || '',
      status: host.status || '',
      error: host.error || '',
      panes: (host.panes || []).map(pane => ({
        id: pane.id, number: pane.number, prefix: pane.prefix || '',
        session: pane.session, window: pane.window, window_name: pane.window_name,
        path: pane.path, title: pane.title || '',
      })),
    }));
    return writeJSON(HOSTS_KEY, {at: Date.now(), hosts: compact});
  }

  function readHosts(maxAge) {
    const data = readJSON(HOSTS_KEY);
    if (!data || !Array.isArray(data.hosts) || !data.hosts.length) return null;
    const age = Date.now() - (data.at || 0);
    if (maxAge && age > maxAge) return null;
    return {hosts: data.hosts, at: data.at || 0, age};
  }

  // ---------------------------------------------------------------- transcripts
  function saveTranscript(id, data) {
    if (!id || !data || !Array.isArray(data.messages) || !data.messages.length) return false;
    const messages = data.messages.slice(-TX_MESSAGES);
    let payload = JSON.stringify({
      at: Date.now(),
      mtime: data.mtime || null,
      provider: data.provider || '',
      sig: data.sig || '',
      messages,
    });
    if (payload.length > PER_TX_CHARS) {          // 单条就超标：从最早的开始丢
      while (payload.length > PER_TX_CHARS && messages.length > 4) {
        messages.shift();
        payload = JSON.stringify({at: Date.now(), mtime: data.mtime || null,
                                  provider: data.provider || '', sig: data.sig || '', messages});
      }
    }
    prune(MAX_TX - 1, TX_PREFIX + id);
    return writeJSON(TX_PREFIX + id, JSON.parse(payload));
  }

  function readTranscript(id, maxAge) {
    if (!id) return null;
    const data = readJSON(TX_PREFIX + id);
    if (!data || !Array.isArray(data.messages) || !data.messages.length) return null;
    const age = Date.now() - (data.at || 0);
    if (maxAge && age > maxAge) return null;
    return {messages: data.messages, mtime: data.mtime, provider: data.provider,
            sig: data.sig, age};
  }

  function clear() {
    try { localStorage.removeItem(HOSTS_KEY); } catch (_error) {}
    for (const key of txKeys()) {
      try { localStorage.removeItem(key); } catch (_error) {}
    }
  }

  window.TmuxPaneCache = {saveHosts, readHosts, saveTranscript, readTranscript, clear};
})();
