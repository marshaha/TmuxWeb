// Unit tests for the agent-transport UI in web/hosts.html.
// Run with: node --test tests/js/agent-transport.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const root = path.dirname(new URL(import.meta.url).pathname);
const hostsPage = fs.readFileSync(path.join(root, '..', '..', 'web', 'hosts.html'), 'utf8');

function grab(page, name) {
  const start = page.indexOf('  function ' + name + '(');
  assert.notEqual(start, -1, `missing function ${name}`);
  let depth = 0;
  for (let i = page.indexOf('{', start); i < page.length; i += 1) {
    if (page[i] === '{') depth += 1;
    else if (page[i] === '}') {
      depth -= 1;
      if (!depth) return page.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${name}`);
}

const transportOf = new Function(grab(hostsPage, 'transportOf') + 'return transportOf;')();
globalThis.transportOf = transportOf;
const formatTime = new Function(grab(hostsPage, 'formatTime') + 'return formatTime;')();
globalThis.formatTime = formatTime;
const agentWsUrl = new Function(grab(hostsPage, 'agentWsUrl') + 'return agentWsUrl;')();
globalThis.agentWsUrl = agentWsUrl;
const transportLabel = new Function(grab(hostsPage, 'transportLabel') + 'return transportLabel;')();
const agentStatusText = new Function(grab(hostsPage, 'agentStatusText') + 'return agentStatusText;')();
const lastTransportText = new Function(grab(hostsPage, 'lastTransportText') + 'return lastTransportText;')();
const agentInstallLabel = new Function(grab(hostsPage, 'agentInstallLabel') + 'return agentInstallLabel;')();
const manualInstallCommand = new Function(grab(hostsPage, 'manualInstallCommand') + 'return manualInstallCommand;')();
const transportSwitchPayload = new Function(grab(hostsPage, 'transportSwitchPayload') + 'return transportSwitchPayload;')();

test('transportOf treats a missing or unknown transport as SSH', () => {
  assert.equal(transportOf({}), 'ssh');
  assert.equal(transportOf(null), 'ssh');
  assert.equal(transportOf(undefined), 'ssh');
  assert.equal(transportOf({ transport: 'ssh' }), 'ssh');
  assert.equal(transportOf({ transport: 'agent' }), 'agent');
  assert.equal(transportOf({ transport: 'pigeon' }), 'ssh');
});

test('transportLabel shows SSH / Agent', () => {
  assert.equal(transportLabel({}), 'SSH');
  assert.equal(transportLabel({ transport: 'ssh' }), 'SSH');
  assert.equal(transportLabel({ transport: 'agent' }), 'Agent');
});

test('formatTime renders a unix timestamp as local HH:MM:SS', () => {
  const ts = 1792000000;
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  assert.equal(formatTime(ts), `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
  assert.match(formatTime(0), /^\d{2}:\d{2}:\d{2}$/);
});

test('agentStatusText shows online, offline with last-seen time, or never connected', () => {
  assert.equal(agentStatusText({ agent_online: true }), '●在线');
  assert.equal(agentStatusText({ agent_online: true, agent_last_seen: 1792000000 }), '●在线');
  const ts = 1792000000;
  assert.equal(agentStatusText({ agent_online: false, agent_last_seen: ts }), `○离线 · 最后见到 ${formatTime(ts)}`);
  assert.equal(agentStatusText({ agent_online: false, agent_last_seen: null }), '○离线 · 未连接过');
  assert.equal(agentStatusText({}), '○离线 · 未连接过');
});

test('lastTransportText shows which transport the last operation used, or nothing', () => {
  assert.equal(lastTransportText({ last_transport: 'agent' }), '上次走 Agent');
  assert.equal(lastTransportText({ last_transport: 'ssh' }), '上次走 SSH');
  assert.equal(lastTransportText({ last_transport: null }), '');
  assert.equal(lastTransportText({}), '');
});

test('agentInstallLabel shows 重新安装 only while the agent is online', () => {
  assert.equal(agentInstallLabel({ agent_online: true }), '重新安装');
  assert.equal(agentInstallLabel({ agent_online: false }), '安装 agent');
  assert.equal(agentInstallLabel({}), '安装 agent');
});

test('agentWsUrl derives ws/wss from the page origin', () => {
  assert.equal(agentWsUrl('https://tmux.122990.xyz'), 'wss://tmux.122990.xyz');
  assert.equal(agentWsUrl('http://10.0.0.8:8787'), 'ws://10.0.0.8:8787');
  assert.equal(agentWsUrl('https://example.com:8443'), 'wss://example.com:8443');
  assert.equal(agentWsUrl('ws://already.example'), 'ws://already.example');
});

test('manualInstallCommand splices the origin, wss server and token into the exact command', () => {
  assert.equal(
    manualInstallCommand('https://tmux.122990.xyz', 'TOKEN123'),
    'curl -fsSL https://tmux.122990.xyz/agent/agent.py -o ~/.tmuxweb-agent/agent.py && python3 ~/.tmuxweb-agent/agent.py install --server wss://tmux.122990.xyz/ws/agent --token TOKEN123');
  assert.equal(
    manualInstallCommand('http://10.0.0.8:8787', 'abc'),
    'curl -fsSL http://10.0.0.8:8787/agent/agent.py -o ~/.tmuxweb-agent/agent.py && python3 ~/.tmuxweb-agent/agent.py install --server ws://10.0.0.8:8787/ws/agent --token abc');
  const cmd = manualInstallCommand('https://example.com', 'tok-9');
  assert.equal(cmd.match(/tok-9/g).length, 1, 'token appears exactly once, in --token');
  assert.ok(cmd.includes('--server wss://example.com/ws/agent'));
});

test('transportSwitchPayload keeps the host fields and only changes transport', () => {
  const host = {
    name: 'build-box', hostname: '10.0.0.8', port: 22, username: 'ubuntu',
    auth_type: 'key', jump_host_id: 'j1', jump_spec: '', host_key: 'hk', fingerprint: 'fp',
    transport: 'ssh',
  };
  assert.deepEqual(transportSwitchPayload(host, 'agent'), {
    name: 'build-box', hostname: '10.0.0.8', port: 22, username: 'ubuntu',
    auth_type: 'key', jump_host_id: 'j1', jump_spec: '', host_key: 'hk', fingerprint: 'fp',
    verify: false, transport: 'agent',
  });
  assert.equal(transportSwitchPayload(host, 'ssh').transport, 'ssh');
  assert.equal(transportSwitchPayload(host, 'pigeon').transport, 'ssh');
  const bare = transportSwitchPayload({ name: 'n', hostname: 'h', port: '2222', username: 'u', auth_type: 'password' }, 'agent');
  assert.equal(bare.port, 2222);
  assert.equal(bare.jump_host_id, '');
  assert.equal(bare.jump_spec, '');
  assert.equal(bare.host_key, '');
  assert.equal(bare.fingerprint, '');
});

test('the list row renders the badge, agent status, last transport, switch and install buttons', () => {
  const start = hostsPage.indexOf('async function load()');
  assert.notEqual(start, -1, 'missing load');
  const body = hostsPage.slice(start, hostsPage.indexOf('function authMode', start));
  for (const marker of [
    'transport-badge ${transportOf(h)}',
    '${transportLabel(h)}',
    '${esc(agentStatusText(h))}',
    '${esc(lastTransportText(h))}',
    'data-transport="ssh"',
    'data-transport="agent"',
    'data-install-agent="${esc(h.id)}"',
    '${agentInstallLabel(h)}',
    'data-manual-install="${esc(h.id)}"',
    'data-manual-panel="${esc(h.id)}"',
    'data-copy-manual="${esc(h.id)}"',
  ]) assert.ok(body.includes(marker), `list row must contain ${marker}`);
  assert.ok(body.includes(".join('')"), 'rows must be joined, not array-stringified');
});

test('switchTransport reuses the edit-host PUT and only swaps transport', () => {
  const start = hostsPage.indexOf('async function switchTransport(');
  assert.notEqual(start, -1, 'missing switchTransport');
  const body = hostsPage.slice(start, hostsPage.indexOf('async function installAgent(', start));
  assert.ok(body.includes("'/api/remote-hosts/'+id"), 'switch must PUT the existing edit-host URL');
  assert.ok(body.includes("method:'PUT'"), 'switch must reuse PUT');
  assert.ok(body.includes('transportSwitchPayload(h,transport)'), 'switch payload comes from transportSwitchPayload');
  assert.ok(body.includes('b.disabled=true'), 'switch must disable the row switch while in flight');
  assert.ok(body.includes('await load()'), 'switch must refresh the list on success');
  assert.ok(body.includes('切换失败'), 'switch must report a failure');
});

test('install and manual install hit the agreed agent endpoints with no token logging', () => {
  const installAt = hostsPage.indexOf('async function installAgent(');
  assert.notEqual(installAt, -1, 'missing installAgent');
  const installBody = hostsPage.slice(installAt, hostsPage.indexOf('async function showManualInstall(', installAt));
  assert.ok(installBody.includes("'/api/hosts/'+id+'/agent-install'"), 'one-click install posts /api/hosts/{id}/agent-install');
  assert.ok(installBody.includes("method:'POST'"), 'install must POST');
  const manualAt = hostsPage.indexOf('async function showManualInstall(');
  assert.notEqual(manualAt, -1, 'missing showManualInstall');
  const manualBody = hostsPage.slice(manualAt, hostsPage.indexOf('async function copyManualCommand(', manualAt));
  assert.ok(manualBody.includes("'/api/hosts/'+id+'/agent-token'"), 'manual install posts /api/hosts/{id}/agent-token');
  assert.ok(manualBody.includes('manualInstallCommand(location.origin,d.token)'), 'command is built from the live origin and the one-time token');
  assert.ok(manualBody.includes('panel.hidden=false'), 'command is shown in the row panel');
  assert.doesNotMatch(hostsPage, /console\.log/, 'the page must never log (a token must never reach the console)');
});

test('add form and edit dialog both carry a transport select', () => {
  assert.ok(hostsPage.includes('<select id="transport"><option value="ssh">SSH</option><option value="agent">Agent</option></select>'), 'add form transport select defaults to SSH');
  assert.ok(hostsPage.includes('id="transport-hint"'), 'add form must hint that agent install happens after save');
  assert.ok(hostsPage.includes('保存后需在列表里安装 agent，对端程序才会回拨'), 'add form hint text');
  assert.ok(hostsPage.includes('<select id="edit-transport"><option value="ssh">SSH</option><option value="agent">Agent</option></select>'), 'edit dialog transport select');
  const saveAt = hostsPage.indexOf('async function saveEdit()');
  assert.notEqual(saveAt, -1, 'missing saveEdit');
  const saveBody = hostsPage.slice(saveAt, hostsPage.indexOf("$('edit-cancel')", saveAt));
  assert.ok(saveBody.includes("transport:$('edit-transport').value"), 'saveEdit must send transport');
  assert.ok(saveBody.includes("method:'PUT'"), 'saveEdit keeps PUT');
  const openAt = hostsPage.indexOf('function openEdit(id)');
  const openBody = hostsPage.slice(openAt, hostsPage.indexOf('async function saveEdit()', openAt));
  assert.ok(openBody.includes("$('edit-transport').value=transportOf(h)"), 'openEdit must restore the host transport');
  const dataAt = hostsPage.indexOf('function data(){');
  const dataBody = hostsPage.slice(dataAt, hostsPage.indexOf('// The host key is pinned', dataAt));
  assert.ok(dataBody.includes('transport:fields.transport.value'), 'add-host payload must send transport');
});

test('the extracted inline script passes node --check', () => {
  const match = hostsPage.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(match, 'inline <script> not found');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hosts-inline-'));
  const scriptPath = path.join(dir, 'inline.mjs');
  try {
    fs.writeFileSync(scriptPath, match[1]);
    const completed = spawnSync('node', ['--check', scriptPath], { encoding: 'utf8', timeout: 60000 });
    assert.equal(completed.status, 0, completed.stderr);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
