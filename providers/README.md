# providers/ — harness provider registry

One module per agent CLI (`claude.py`, `codex.py`, `kimi.py`,
`hermes.py`), plus `__init__.py` which assembles them into
`ProviderSpec` records. This is the single source of truth for
everything TmuxWeb knows about a harness:

| slot | what it is |
|---|---|
| `command_prefixes` / `bins` | detection: `provider_for_command`, `provider_from_argv` |
| `resolve_session_file(home, cwd, title, screen, hint)` | locate the session file (or `None`) |
| `idle_markers` / `busy_markers` | status substrings in the session file |
| `outstanding(records)` | a tool call issued with no result yet |
| `gated` | the provider opts into `needs_input` only with a real decision signal (hermes: `False`) |
| `hook_template` / `hook_waiting` / `hook_clearing` | hook config file + which event names mean waiting/clearing |
| `model_presets` / `model_switch_template` / `provider_label` | the `/model` picker |
| `resume_template` | restart/resume flags — **interface only** (PR #23 plugs in here later; no restart feature in this PR) |
| `transcript_source` | how the transcript is read — see below |

The server imports the registry directly (`agent_status.py`,
`needs_input.py`, `agent_catalog.py`, `agent_models.py`).

## Why the agent program and transcript_reader.py don't import it

Two consumers cannot import `providers/`:

- **The agent program** (`plugins/agent/agent.py`) is deployed as a
  **single file** — served at `/agent/agent.py` and uploaded alone
  over SSH. A sibling module would never arrive. So the provider
  facts travel *inside* `agent.py` as a generated block:
  `scripts/generate_agent_provider_facts.py` renders the data
  (markers, hook sets, bins, gating, resume templates,
  transcript sources) plus the code-shaped functions
  (`<name>_outstanding`, `<name>_resolve_session_file` and their
  helpers, copied verbatim from the provider modules) between
  `# --- BEGIN GENERATED PROVIDER FACTS ---` and the matching `END`
  marker. The watcher code below the block is a small generic engine
  over that data — it holds no per-provider strings of its own.
  Regenerate after any registry change:
  `python3 scripts/generate_agent_provider_facts.py`.
  `tests/test_provider_registry.py::DriftTests` regenerates the block
  in memory and fails if the copy embedded in `agent.py` differs, so
  a hand edit to either side cannot silently drift.

- **The SSH-piped transcript reader** (`transcript_reader.py`) is
  piped over SSH to `python3 -` on remote hosts, so it must stay a
  single shippable unit, stdlib-only and py3.8-compatible. It keeps
  its own copies of detection and session resolution, and the same
  drift test pins them: detection and session-path results on a
  corpus of commands, argv lines and fake homes must equal the
  registry's, or the test fails.

The provider functions themselves are written in conservative,
stdlib-only Python (no 3.9+ syntax) for exactly this reason — they
run on hosts with older interpreters. Keep them that way.

## The POSIX-shell mirror

`agent_status.STATUS_SHELL` (used by `remote_hosts.py` over SSH on
hosts without python3) is *built* from the registry's markers at
import time, so the shell's status rules cannot drift from the
Python ones. Its `decision_of` (outstanding-call check) is still a
hand-written approximation — it only checks the *newest* call, while
the Python side tracks every id — and is covered by the
shell-agreement tests in `tests/test_agent_status.py` instead.

## transcript_source

The slot must express every shape a harness transcript can have, not
just the file-path shape the four current providers use:

- `{"kind": "jsonl", ...}` — a session file (claude, codex, kimi).
- `{"kind": "sqlite", "db": ..., "tables": {...}}` — a state
  database (hermes `~/.hermes/state.db`).
- `{"kind": "cli", "commands": [...]}` — a CLI export.

A provider lists sources in priority order (hermes: sqlite first,
per-session jsonl fallback). `validate_transcript_sources` enforces
the shape; the registry tests prove an OpenCode-shaped spec
(sqlite + CLI export) validates, without adding OpenCode as a
provider.

## What is deliberately NOT a provider here

- **OpenCode** — transcript source is a second shape (single SQLite
  `~/.local/share/opencode/opencode.db`, tables
  `session`/`message`/`part`, 2.x
  `session_v2`/`session_message`/`session_part`, payload JSON in a
  data column, schema undocumented/unstable; old
  `storage/session/{project}/{session}.json` files are dead
  post-migration; supported reads are `opencode export <id>` /
  `opencode session list --format json` / `opencode db path`). The
  interface expresses it; the implementation waits until real-host
  verification. On 2026-11-16 no host had the binary or the database
  (`command -v opencode` failed on localhost, oracle, macmini,
  aliyun, ctyun; no `~/.local/share/opencode/opencode.db`
  anywhere).
- **Gemini CLI** — pilot run first, before any Gemini code
  (2026-11-16, read-only `/api/exec` checks on all five hosts):
  no working binary on any host — `command -v gemini` failed
  everywhere, and on ctyun `/usr/bin/gemini` is a **broken symlink**
  to the uninstalled npm `@google/gemini-cli` bundle. Real session
  files *do* exist on ctyun only (5 files under
  `~/.gemini/tmp/*/chats/`, Apr–Jul 2026), and their format was read
  from those real files: JSONL with a `sessionId`/`projectHash`
  header record, `$set` snapshots, and `user`/`gemini`/`info`/`error`
  records — but the newest session (2026-07-09) ends waiting on a
  Google OAuth page, consistent with Gemini CLI having stopped
  serving free/Google One users. With no live binary there is nothing
  that can start a new session, so Gemini stays catalog-only (bins in
  `agent_catalog.py`, honored by no other layer) and is NOT added to
  this registry.

## Adding a harness

1. One provider module in `providers/` (copy `kimi.py`): facts +
   the two code-shaped functions. Verify every session format, hook
   field and resume flag on a real machine/file first — never from
   memory or docs alone.
2. Its hook template in `plugins/agent/hooks/` (if the harness has
   hooks), named in `hook_template`.
3. Tests in `tests/test_provider_registry.py`: detection, session
   path, markers, outstanding-call, gating — resolving exactly as
   before the registry existed.
4. Run `python3 scripts/generate_agent_provider_facts.py` to refresh
   the agent's embedded block, then full pytest + node.

## transcript 读取

读聊天记录的差异（远端要不要反转行序、模型名在哪）声明在
`transcript_reader.ADAPTERS` 里，见 `docs/transcript-adapters.md`；
本目录负责"怎么认这个 agent"，那边负责"怎么把它的记录读出来"，两边有断言绑定。
