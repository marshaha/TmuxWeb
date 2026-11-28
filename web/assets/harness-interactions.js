/* Hints select a read path. Only the provider-owned reply supplies a form. */
(() => {
  const codex = {
    name: 'Codex',
    notice: signal => ['codex_followup','codex_tool_permission','codex_screen_notice'].includes(signal?.source),
    available: ({signal,followup,openCodex}) => codex.notice(signal) || followup || openCodex,
    read: ({signal,followup}) => followup || (codex.notice(signal) ? signal : null)
  };
  const hermes = {
    name: 'Hermes',
    notice: signal => signal?.source === 'hermes_clarify' || signal?.kind === 'notice' &&
      signal.options?.some(o => ['需要回答问题','等待操作审批'].includes(o.label)),
    available: ({pane,transcript,signal}) => pane.provider === 'hermes' || pane.command === 'hermes' ||
      transcript === 'hermes' || hermes.notice(signal),
    read: ({signal}) => hermes.notice(signal) ? signal : null
  };
  const kimi = {
    name: 'Kimi',
    available: ({pane,signal}) => pane.capabilities?.native_form === 'owner' || pane.capabilities?.native_form === 'view' && signal,
    read: ({signal}) => signal || null
  };
  window.TmuxHarnessInteractions = {
    hermes, codex,
    select(context) {
      if (!context.pane) return null;
      if (context.pane.provider === 'kimi') return kimi.available(context) ? kimi : null;
      return [codex,hermes].find(adapter => adapter.available(context));
    }
  };
})();
