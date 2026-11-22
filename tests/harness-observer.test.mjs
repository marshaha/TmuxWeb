import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { test } from 'node:test'
import vm from 'node:vm'

test('OpenCode observes just the session displayed by this TUI and clears on exit', () => {
  const source = readFileSync(new URL('../plugins/agent/hooks/opencode/tui.js', import.meta.url), 'utf8')
    .replace(/^import .*$/gm, '').replace('export default ', '')
  const messages = []
  let tick, stopped = false, route = {type: 'session', sessionID: 'mine'}
  const plugin = vm.runInNewContext(source, {
    Plugin: {define: plugin => plugin}, process: {env: {TMUX_PANE: '%4'}}, homedir: () => '/fixture',
    join: (...parts) => parts.join('/'), setInterval: cb => { tick = cb; return 42 },
    clearInterval: id => { stopped = id === 42 },
    spawn: () => {
      const child = new EventEmitter()
      child.stdin = new EventEmitter()
      child.stdin.end = json => { messages.push(JSON.parse(json)); child.emit('close', 0) }
      return child
    },
  })
  const stop = plugin.setup({
    ui: {router: {current: () => route}},
    data: {session: {permission: {list: session => {
      assert.equal(session, 'mine')
      return [{id: 'p', hidden: 'do not export'}]
    }}, form: {list: () => [{id: 'f'}]}}},
  })
  tick()
  tick()
  assert.deepEqual(messages, [{session_id: 'mine', items: [
    {id: 'p', kind: 'permission'}, {id: 'f', kind: 'form'},
  ]}])
  route = {type: 'home'}
  tick()
  assert.deepEqual(messages.at(-1), {session_id: '', items: []})
  stop()
  assert.ok(stopped)
})
