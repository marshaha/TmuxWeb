# Host-side "needs you" watcher

Stacks on PR #24. PR #24's server gate is a three-legged conjunction
(decision point + stable screen + parsed menu) and it stays the
fallback; this watcher is the precision layer that lives on the host,
next to the session files and the tmux server, so it sees transitions
in seconds instead of at sweep cadence (~1 minute). It feeds the same
gate: the sweep merges agent-reported waiting state with its own
decision point, the agent event winning when present.

## How it works

The agent program (`plugins/agent/agent.py`, stdlib-only, single
file, now version 1.1.0) gains a `HostWatcher`:

1. **Hook events (exact).** Hook scripts — one shared stdlib script,
   `plugins/agent/hooks/tmuxweb-hook.py` — append one JSON line per
   event to `~/.config/tmuxweb-agent/events.jsonl` and exit 0. The
   watcher tails that file. A permission event sets waiting; a
   result/stop event clears it. Hooks never dial out and never block
   the agent (write + exit 0, 5s timeout). The event file is capped
   at 256KB / 1000 lines; events older than 1 hour expire.
2. **tmux control mode (screen-quiet).** A persistent `tmux -C`
   client owned by the agent tracks the last `%output` time per pane.
   Quiet (no output for 5s) + a session-file decision point means the
   agent is sitting at a prompt it did not announce by hook —
   question prompts and elicitation, which carry no permission event.
   A dead control client degrades to "no control signal", never kills
   the agent. It only starts when the host actually has tmux sessions
   (starting `tmux -C` on a host with no server would create a stray
   session), and it attaches to the session of the pane being judged —
   one session at a time, so panes in other sessions fall back to the
   file + hook legs.
3. **Session files (decision point).** High-frequency mtime polling
   (2s background loop, plus a fresh read on every `needs_input` op)
   of the same files `agent_status.py` reads: Claude
   `~/.claude/projects/<dashified-cwd>/*.jsonl`, Codex
   `~/.codex/sessions/**/*.jsonl`, Kimi
   `~/.kimi-code/sessions/wd_*/session_*/agents/main/wire.jsonl`,
   with the outstanding-call predicates ported (the agent is deployed
   standalone and cannot import server modules).

Waiting is: hook event says so (exact), OR decision point AND screen
quiet (inferred). A decision point alone is **not** waiting — that is
the server gate's job, and duplicating it here would duplicate its
false-positive mode.

The agent exposes it as a new op, `{"op": "needs_input", "panes":
[...]}`, answering per-pane `{waiting, source: hook|control-mode,
since, decision_point, control_quiet, hook}`. The server
(`AgentTransport.needs_input_many`, one extra batch per agent host
per sweep) merges it into leg 1 of the gate. Federation transports
are never asked.

## Per-provider hook evidence and verification method

| Provider | Hook | Verified how |
|---|---|---|
| Claude Code | `Notification`, matcher `permission_prompt` (+ `elicitation_dialog`), + `Stop` | Official docs, code.claude.com hooks guide, read 2026-11-16: common fields `session_id` / `transcript_path` / `cwd` / `hook_event_name`; `Notification` adds `message` / `title` / `notification_type`. `permission_prompt` fires ~6s into the wait. |
| Codex 0.165.0 | `~/.codex/hooks.json`, `PermissionRequest` + `Stop` | Binary strings on the installed 0.165.0 contain `hooks.json`; payload field names (`session_id` / `transcript_path` / `cwd` / `hook_event_name` / `model`, turn events add `turn_id`) are the ones several independent sources agree on — no official page was reachable from the sandbox, so the parser also accepts `sessionId` / `thread_id` aliases and the hook command carries the event name as argv[1]. The older `notify` key fires only on agent-turn-complete, never on approval. Hooks run only after review/trust via `/hooks` in the TUI; re-trust after every change. |
| Kimi 2.3.1 | `[[hooks]]` in `~/.kimi-code/config.toml` (exactly `event` / `matcher` / `command` / `timeout`), `PermissionRequest` / `PermissionResult` / `Stop` | Official docs, kimi.com/code/docs hooks page, read 2026-11-16: JSON on stdin with `hook_event_name` / `session_id` / `session_title` / `client_type` / `cwd`; `PermissionRequest` is "just before waiting for user approval", observation-only, fail-open. Installed 2.3.1 binary strings contain `fireAndForget("PermissionRequest", ...)`. |
| Hermes | — | No user-facing TUI hook (`config.yaml` has zero hook lines). Its precise channel is ACP (`session/request_permission`), which needs ACP driving, not the TUI — out of scope. Hermes stays on the server-side gate; the watcher never reports it waiting. |

## Honest limits

- Hook events are exact for permission prompts. Question prompts /
  elicitation and generic "waiting for input" without a permission
  event are still inferred from control-mode quiet + outstanding
  call, and inherit that inference's false-positive mode (a
  long-running tool whose TUI sits frozen on a menu-like screen).
  The card still never answers on its own — a human taps.
- Control-mode coverage depends on the tmux server the agent can
  reach: one attached session at a time; panes in other sessions get
  no quiet signal. A host with no tmux sessions gets no control-mode
  client at all.
- Claude's `permission_prompt` notification fires ~6s into the wait,
  so the hook leg lags the prompt by seconds; the file + quiet legs
  can fire sooner.
- Old agents (pre-1.1) answer the new op with an error result; the
  server treats that as "no agent signal" and the PR #24 gate stays
  the fallback. SSH-only hosts and Hermes never get an agent signal.
- The watcher reports waiting; the gate still needs a stable screen
  and a parsed menu before a card lights up. An idle pane with no
  menu never lights up from the watcher alone.

## Deployment (manual — nothing in this PR installs anything)

1. Deploy agent 1.1.0 to each host (normal agent update path).
2. Copy `plugins/agent/hooks/tmuxweb-hook.py` to
   `~/.config/tmuxweb-agent/hooks/` on each host and `chmod +x` it.
3. Per provider, install the template in
   `plugins/agent/hooks/README.md` (Claude `settings.json` fragment,
   Codex `hooks.json`, Kimi `config.toml` entries) and trust it
   (Codex: `/hooks`, re-trust after every change; Claude: check
   `/hooks`; Kimi: start a new session).
4. Verify on the host: `tail ~/.config/tmuxweb-agent/events.jsonl`
   shows one line per event while a permission prompt is up, and a
   clearing line after it is answered.
