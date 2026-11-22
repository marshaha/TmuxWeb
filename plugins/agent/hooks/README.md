# Host-watcher hooks — install manually, per host

These files are **templates only**. Nothing in this PR installs them:
installing a hook changes what runs inside a real agent session, so
it is a deployment action the user does by hand, host by host, after
the agent 1.2.0 is deployed. The agent works without hooks (the
session-file + control-mode legs still report); hooks only make the
signal exact.

## What to copy

Copy the shared receiver to every host that runs the agent:

```
mkdir -p ~/.config/tmuxweb-agent/hooks
cp plugins/agent/hooks/tmuxweb-hook.py ~/.config/tmuxweb-agent/hooks/
chmod +x ~/.config/tmuxweb-agent/hooks/tmuxweb-hook.py
```

The receiver appends one JSON line per event to
`~/.config/tmuxweb-agent/events.jsonl` and always exits 0. It never
dials out and never blocks the agent. If the agent was installed
with a different config dir, set `TMUXWEB_AGENT_EVENTS` in the hook
command's environment to the same path the agent reads.

## Claude Code — `~/.claude/settings.json`

Merge the `hooks` block from `claude-settings.json` into
`~/.claude/settings.json` (it is a fragment, not a whole file):

- `Notification`, matcher `permission_prompt|elicitation_dialog` —
  fires when Claude is waiting for approval (about 6s into the
  wait) or on an elicitation dialog. Sets waiting.
- `Stop` — fires when a turn ends. Clears waiting.

Verify with `/hooks` in Claude Code; the two entries must appear.
Payload fields (official docs, code.claude.com hooks guide):
`session_id`, `transcript_path`, `cwd`, `hook_event_name`;
`Notification` adds `message`, `title`, `notification_type`.

## Codex — `~/.codex/hooks.json`

Copy `codex-hooks.json` to `~/.codex/hooks.json`:

- `PermissionRequest` — fires when approval is needed. Sets waiting.
- `Stop` — a turn finishes. Clears waiting.

Then **trust the hooks**: run `/hooks` in the Codex TUI and trust
each new hook — and again after every change to the file. Until
trusted, Codex silently never runs them. (Project-level
`.codex/hooks.json` additionally needs a trusted project.)
Payload fields (several independent sources agree; no official page
was reachable when this was written, and the parser accepts
`sessionId` / `thread_id` aliases): `session_id`,
`transcript_path`, `cwd`, `hook_event_name`, `model`; turn events
add `turn_id`. The older `notify` key is NOT used: it fires only on
agent-turn-complete, never on approval.

## Kimi Code — `~/.kimi-code/config.toml`

Append the `[[hooks]]` entries from `kimi-hooks.toml`:

- `PermissionRequest` — "just before waiting for user approval".
  Sets waiting. Observation-only, fail-open.
- `PermissionResult` — after approval completes. Clears waiting.
- `Stop` — the turn is about to end. Clears waiting.

Start a new session for the hooks to take effect. Payload fields
(official docs, kimi.com/code/docs hooks page): JSON on stdin with
`hook_event_name`, `session_id`, `session_title`, `client_type`,
`cwd`. `[[hooks]]` takes exactly four fields: `event`, `matcher`,
`command`, `timeout`.

## Native Chat interaction notices

Current Hermes CLI and TUI versions expose human-input hooks. The native Chat integration for Hermes and five other harnesses has its own receiver and installation fragments. See [Chat interaction sources](../../../docs/chat-interactions.md).

## Checking it works

After the hooks are in place, run an agent into a permission prompt
and check the event file on that host:

```
tail -n 5 ~/.config/tmuxweb-agent/events.jsonl
```

One line per event, `{"session_id": ..., "hook_event_name":
"permission_prompt", "timestamp": ...}`. Answer the prompt and a
`Stop` / `PermissionResult` line must follow. If no lines appear,
the hook is not trusted (Codex) or not registered (Claude `/hooks`,
Kimi new session) — the watcher degrades to its other two legs.
