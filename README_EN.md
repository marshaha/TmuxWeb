# TmuxWeb

[中文](README.md) · [Testing](TESTING_EN.md) · [测试与验收](TESTING.md)

TmuxWeb is a self-hosted control center for local and remote tmux sessions and AI CLI conversations. It organizes panes as `host → session → window → pane` and provides a ChatGPT-style conversation view, a real PTY terminal, structured transcripts, and controlled session sharing.

> Security notice: TmuxWeb can type into tmux panes and expose an interactive terminal. This is equivalent to access as the operating-system account running the service. Do not expose it directly to the Internet. Use HTTPS, access controls, a strong password, and a firewall.

## Features

- **Multi-host control plane**: one local service manages localhost and SSH hosts; remote machines only need SSH and tmux.
- **Host form**: 主机/IP gets its own full-width row; a 显示凭据内容 checkbox reveals the password (and unmasks the private
  key); 不校验连接直接保存 stores a host even when the password/key is wrong or the host is unreachable - it shows as offline
  with a pending fingerprint, which is pinned automatically on the first successful connection (TOFU,
  `StrictHostKeyChecking=accept-new`) and enforced from then on.
- **tmux on macOS**: a non-interactive ssh command does not see Homebrew's PATH, so the first connection resolves the binary
  to an absolute path (PATH → /opt/homebrew/bin → /usr/local/bin → ~/bin → `bash -lc` / `zsh -lc`), caches it, and exports
  `TMUX_BIN` for every remote command; a genuinely missing tmux reports `brew install tmux`, and `TMUX_WEB_TMUX_PATH` overrides the lookup.
- **Tmux tree**: host/session/window/pane navigation with persisted expansion state.
- **AI chat view**: structured transcripts for Claude Code, Codex, Hermes, and Kimi Code on localhost *and* SSH hosts, with ANSI `capture-pane` fallback.
- **Cross-server relay**: an agent in any pane can ask an agent pane on another host and receive its answer through the HTTP API or `relay_client.py`.
- **Remote rendering performance**: SSH connection multiplexing, remote content digests, idle backoff — and non-ASCII pane titles no longer arrive as `_`.
- **File panel**: browse the selected pane's working directory in the sidebar (enter/leave folders, scroll), download with a click and upload by drag-and-drop, on localhost and over SSH alike.
- **Message actions**: every message has a "复制全部" (copy all) button; each block inside a message (code / mermaid / table) has its own toolbar — copy the block (tables copy as TSV, mermaid copies the source) and, for tables and mermaid, flip between source and rendered. Mermaid is vendored locally and loaded lazily, only when a message actually contains a mermaid block.
- **Real terminal**: xterm.js over a PTY with input, resize, ANSI, and mobile shortcut controls.
- **Literal input delivery**: messages use tmux buffers instead of shell interpolation and account for stale drafts and bracketed paste.
- **New sessions**: the "+" next to a host row runs `tmux new-session -d` on that host (local or SSH), with a
  dialog for the session name (letters, digits, `.`, `-`, `_`, 1–64 chars) and an optional working directory; the
  new pane is expanded and selected afterwards, and a missing directory is rejected up front.
- **Remote SSH**: password, private-key, or "local keys / SSH Agent" authentication (no credential at all when the remote already authorises your public key), pinned host-key fingerprints, and Fernet-encrypted credentials at rest.
- **Conversation sharing**: slug, password, expiration, extension, password reset, stop, and delete controls, plus a **read-only vs. interactive** choice. **Any pane** can be shared: agent sessions render as messages, plain shell panes render as screen text and still accept input.
- **Mobile/PWA support**: visual viewport and safe-area handling; sensitive API and transcript data are never Service Worker cached.
- **Icons**: `web/app-icon.svg` is the single source (dark rounded tile, cream `>`, terracotta block cursor); `scripts/build_icons.py` renders the 32/180/192/512 PNGs plus a maskable variant. Every page — including login — links the favicon, and the manifest declares `any` and `maskable` separately so Android's circular crop never clips the mark.
- **File panel**: the path bar is editable (absolute, or relative to the pane cwd — Enter navigates, Esc cancels, focus selects the whole path). the toolbar download button zips the current directory (or just the selection when there is one); every row carries a checkbox, `Ctrl/⌘+click` toggles one item and `Shift+click` selects a range (desktop conventions), and the footer offers 全选 / 清空 / 下载 zip (N) for files *and* whole directories can be zipped: local panes zip straight from disk, remote hosts only need `tar` (the bytes come back in one SSH call and are re-containered locally). Limits: `TMUX_WEB_ZIP_MAX` (128MB uncompressed) and `TMUX_WEB_ZIP_MAX_FILES` (1000).
- **Optional MCP mailbox**: controlled text messaging among Claude Code, Codex, and Hermes without arbitrary shell or filesystem tools.

## Design boundaries

- Chat mode never keeps a `tmux attach` client alive, avoiding terminal-size interference between devices.
- Only terminal mode attaches through a PTY; the attachment is released after WebSocket disconnect.
- Remote structured transcripts need no remote `python3` either: a POSIX shell snippet locates the session file (Claude project dir + title/screen probe, Codex `session_index`, Kimi `state.json`, Hermes `--resume` session jsonl or `sqlite3`), the bytes are pulled here and parsed by the **same local parsers** — the `scp` model. When discovery fails the pane falls back to `capture-pane`. Remote transcripts are polled at the same 1s cadence: the server caches the parsed result per (host, pane, limit), serves repeats for 2s without touching the host, then uses a single `stat` probe and only re-pulls when the file changed — so a remote conversation follows the remote file live at almost no cost while idle.
- The UI caches each pane's transcript, scroll offset and unsent draft: switching back paints instantly from cache
  (0.02-0.14s measured) and revalidates in the background, repainting only when the signature (mtime + message count +
  last id) changes. The working set is capped at eight panes.
- Remote sessions are re-located periodically: the client pins the session file and reads only new bytes, but an agent that
  starts a new session in the same pane writes a new file. Every tail now also reports the pane title, and a changed title
  (or `TMUX_WEB_TRANSCRIPT_RELOCATE` seconds, 30s by default) triggers a fresh lookup instead of tailing the stale file.
  The refresh buttons in the web UI and the app send `refresh=1`, which drops the incremental state and cache.
- Remote transcript transfer is incremental and compressed: the first lookup scans only the tails of the newest three
  session files (never the whole 300 MB log), later polls read just the newly appended bytes (`tail -c +offset`), and the
  payload is gzipped on the wire. Measured on a host with a 300 MB transcript and 0.7s RTT: first load 53s -> 8.6s, later
  polls ~1.5s, per-poll transfer down from 4 MB to tens of bytes or a few hundred KB. Window size: `TMUXWEB_PAYLOAD_BYTES`.
- Remote pane references are host-scoped so `%0` from different tmux servers cannot collide.
- Remote tmux calls force `LC_ALL=C.UTF-8` and use a printable field separator, because tmux rewrites control characters and non-ASCII format fields to `_` outside a UTF-8 locale.
- Live views poll a remote `cksum` digest first and only transfer pane text when it changed; the interval backs off to 2s while idle.
- The current administration model is single-user. Multi-user deployments still require per-user host authorization and auditing.

## Files

The 「▤」 button in the sidebar switches to a file view of the selected pane's working directory; press it again (or 「✕」 inside the panel) to put the panel away and get the pane tree back. The choice is remembered in the browser, and a reload restores both the panel state and the last selected pane.

The 「▤」 button in the sidebar switches to a file view of the selected pane's working directory:

- Folders first, click to enter, `..` to go up; hidden entries are hidden by default.
- Click a file to download it (`Content-Disposition: attachment`, UTF-8 names supported).
- Drop files on the panel to upload into the current directory, or use the 「⇧」 button to pick several.
- Local panes read the filesystem directly; SSH panes use POSIX tools only: `find` for listings (GNU
  `-printf` when available, GNU/BSD `stat` otherwise), `head -c` to download, `cat >` to upload. No remote
  `python3` and nothing to install — the same dependency level as `scp`.
- Uploads go to a temporary file and are renamed into place, so a dropped file never truncates an existing one;
  limits default to 64 MB down and 256 MB up (`TMUX_WEB_FILE_DOWNLOAD_MAX` / `TMUX_WEB_FILE_UPLOAD_MAX`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/panes/{pane}/files?path=&hidden=&limit=` | list a directory (defaults to the pane cwd) |
| GET | `/api/panes/{pane}/files/download?path=` | download one file |
| PUT | `/api/panes/{pane}/files/upload?dir=&name=` | raw request body becomes the file |

> File access equals the permissions of the account running the service, on remote hosts too.

## Cross-server relay

Any process that can reach TmuxWeb can relay, which is how an agent on host A asks an agent on host B:

```bash
export TMUXWEB_URL=https://tmux.example.internal
export TMUXWEB_TOKEN=...              # matches TMUX_WEB_API_TOKEN in .env
chmod +x relay_client.py

./relay_client.py peers               # agent panes on every host, with idle/busy
./relay_client.py ask '%12' 'summarize the deployment conclusion'
./relay_client.py ask 'build01~%3' 'did the unit tests pass?' --timeout 300
./relay_client.py send 'build01~%3' 'run the unit tests first'
```

Pane references are `%12` (localhost) or `<host_id>~%3` (remote; `peers` prints both).

HTTP endpoints (`Authorization: Bearer $TMUX_WEB_API_TOKEN`):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/relay/peers` | every pane with provider and `idle`/`busy` status |
| GET | `/api/panes/status` | compact status feed for the UI |
| POST | `/api/relay/ask` | `{target, question, timeout, force, source}`, returns `reply` |
| POST | `/api/relay/message` | send without waiting |
| GET | `/api/relay/log` | last 100 relay exchanges (in-memory) |

Protocol: TmuxWeb embeds a one-time marker and asks the target agent to print
`TMUXWEB_REPLY_<token>: "<json string>"` as its last line, then polls local or remote capture every 0.8s until it matches or the request times out (504 returns the pane tail). A busy pane is refused with 409 unless `force=true`.

## Requirements

- Linux or macOS
- Python 3.11+
- tmux
- OpenSSH client and `ssh-keyscan` for remote hosts
- Optional: Claude Code, Codex, Hermes, and/or Kimi Code CLI

## Install

```bash
git clone https://github.com/marshaha/TmuxWeb.git
cd TmuxWeb
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Generate a login password and a stable session secret:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Put the results in `.env`:

```dotenv
TMUX_WEB_PASSWORD=replace-with-a-long-random-password
TMUX_WEB_SESSION_SECRET=replace-with-a-64-character-hex-value
TMUX_WEB_HOST=127.0.0.1
TMUX_WEB_PORT=8787
```

`.env`, databases, SSH credentials, and encryption keys are ignored by Git. Never commit them.

## Run

```bash
chmod +x start_tmux_web.sh
./start_tmux_web.sh
```

Open `http://127.0.0.1:8787`.

Run it inside tmux:

```bash
tmux new-session -d -s tmuxweb "$PWD/start_tmux_web.sh"
tmux capture-pane -p -t tmuxweb -S -100
tmux kill-session -t tmuxweb
```

To listen on all interfaces, set:

```dotenv
TMUX_WEB_HOST=0.0.0.0
```

Only do this behind a firewall and an HTTPS reverse proxy or secure tunnel. The proxy may use plain HTTP to the localhost origin after terminating TLS.

Administrator and share-password logins are rate limited by client address (five failures per 60 seconds by default). Cookies automatically use `Secure` on HTTPS requests. Set `TMUX_WEB_SECURE_COOKIES=true` if a TLS proxy does not preserve the request scheme.

## Add a remote host

Open `/hosts` and follow the staged flow:

1. Enter host, port, and username.
2. Choose password or private-key authentication.
3. Test the connection — the SHA256 host-key fingerprint is scanned, pinned and shown under the form (no confirmation box).
(The password field is masked by default; the eye button reveals it.)
5. Test SSH and remote `tmux list-panes` without saving.
6. Save only after the test succeeds.

Passwords and private keys are not returned by list APIs. A private key must be a complete OpenSSH/PEM private key, not a one-line `ssh-ed25519 AAAA...` public key. Passphrase-encrypted private keys are not currently supported.

## Performance: a firehose pane must not freeze the browser

Log panes (uvicorn, `tail -f`, `make`, a `while true` loop) emit thousands of lines per second. Selecting one used to rebuild the
whole screen DOM, re-fit the font on every frame and let the xterm write queue grow without bound, which locked up Chrome.

- the live screen stream (`/ws/panes/{pane}`) sends 1500 lines on the first frame and 800 thereafter; a frame larger than 192KB keeps
  only the tail with a "输出过快，已跳过前面 N 字节" marker; idle panes back off from 0.7s to 3s
- the client repaints at most every 500ms, renders only the last 600 lines, keeps just the newest pending frame, and stops re-fitting
  the font unless the container width changed
- terminal mode batches xterm writes into one rAF and, past a 1.5MB backlog, drops the oldest data with an inline notice
- a **pause / resume live** button in the top bar freezes the stream without losing the screen
- `/api/hosts` serves a 5s snapshot (`?refresh=1` re-probes); the sidebar refreshes every 20s and skips identical DOM
- knobs: `TMUX_WEB_STREAM_LINES`, `TMUX_WEB_STREAM_FIRST_LINES`, `TMUX_WEB_STREAM_MAX_BYTES`, `TMUX_WEB_HOSTS_TTL`

Measured on a 140x40 `while true; do echo; done` pane over 10s: chat-mode main-thread time 0.69s -> **0.09s**, rendered text
363k -> 55k chars; 0.02s while paused.

## Jump hosts, and letting an agent add servers

A saved host can be dialled **through another saved host** (`jump_host_id`):

- implemented with `ProxyCommand` + an inner `ssh -W %h:%p`, so **each hop keeps its own credential**; with passwords an
  askpass script picks the right secret by the hostname in the prompt
- the hop's host key is pinned like any other (TOFU the first time, strict afterwards)
- **multi-hop chains** are supported (A -> B -> C, up to `TMUX_WEB_JUMP_MAX_DEPTH`, default 5), each hop with its own
  credential, nested as `ProxyCommand=ssh -W %h:%p ... -o ProxyCommand=ssh -W %h:%p ...`; pointing at itself, at an unknown
  id, or forming a loop is rejected with 422
- deleting a hop falls back to a direct connection instead of leaving a dangling reference
- the host list shows the whole 经由 B -> A path, the edit dialog can change the first hop, and `GET /api/remote-hosts` returns
  `jump_chain` / `jump_path`
- **a jump host does not have to be saved first**: pick one from the list or type `user@host:port` (or just `host`) in the
  field below it. A typed hop reuses the target's own credential, so a shared key/password is stored once; give the bastion a
  different credential by saving it as a host and referencing it instead. Supplying both forms at once is rejected with 422.

### HTTP / MCP

`POST /api/remote-hosts` accepts `jump_host_id`. MCP adds `list_remote_hosts`, `add_remote_host` (name, hostname,
username, port, auth_type, credential, `jump_host`, verify) and `remove_remote_host`, so an agent can register a box itself —
`auth_type=agent` needs no secret at all, and `verify=false` stores a host that is not reachable yet.


## Home board: favourites, cards and search

Every pane in the console sidebar has a star on the right: hollow = not saved, filled = saved. Starred panes appear on `/home`.

`/home` keeps a **collapsible session tree** on the left, the same host → session → window → pane hierarchy as the console,
with busy/idle dots and a star per pane (the collapsed state is remembered; at ≤860px it becomes a drawer with a scrim).
The right side is the board:

- **Favourite cards** — each card can be renamed (pencil → type a name, or 恢复默认 to go back). The default name is
  **session · window · host**, because the tmux pane title is often just the hostname. Each card also carries a **full preview** button: on a desktop it slides a drawer in from the right holding the complete chat page
(markdown/code/tables, can keep sending, can stop, refreshes every 2s); on narrow screens it navigates to `/preview?pane=…` so
the back gesture returns to the board. Hovering a card floats the **full text of the last agent reply** (the panel scrolls with
the wheel) (when the newest message is the user's, it walks back to the
previous agent turn; capped at 6000 chars). Plus a status badge (busy / idle / offline, "thinking…" with a pulse while a turn is unanswered), provider,
  title, path, a **preview of the last message**, and the pane reference. Clicking a card jumps back to the console with that pane
  selected (`/?pane=<pane>`).
- **Board cards** — notes, results and alerts an agent published through the `publish_home_card` MCP tool; removable in the UI.
- **Search** — one box, four groups in priority order: servers, tmux sessions, panes, then the recent messages of every agent pane and finally board cards (hits are highlighted).
  A query containing regex metacharacters runs as a case-insensitive regex; a plain word stays literal, and a broken pattern falls back to a literal search
  (`messages_partial` / `indexing` in the response tell the UI when results are still being scanned). The header keeps only the search box — servers, shares
  and refresh sit in the sidebar header, exactly like the console.
- **Stats** — panes / busy / idle / favourites / cards, refreshed every 15 s; single column on phones.

### Let an agent post to the board

Three MCP tools, available both through the stdio server and HTTP `/mcp`:

| Tool | Purpose |
| --- | --- |
| `publish_home_card` | `title` (required), `body`, `pane`, `kind` (note/result/alert/link) → renders a card on `/home` |
| `list_home` | read the board: favourites with status and last message, plus cards |
| `remove_home_card` | delete a card by id |

```
publish_home_card(title="eth task converged", body="ITER5 notes written, waiting for review", kind="result")
```

Favourites and cards live in `TMUX_WEB_HOME_DB` (default `home.db`) and do not depend on tmux at all.

## Mobile client (native Flutter)

`client/` is a **native** Flutter client (Android / iOS) that talks to the HTTP API directly - no WebView. The web UI keeps
evolving on its own.

- login with server URL + password (session cookie) or `TMUX_WEB_API_TOKEN`; the URL is remembered, the password never is
- host / pane tree with provider badges and screen-text fallback for plain shells
- Local file links inside a conversation are rewritten at render time: `/abs/path`, `~/path`, `file://…` and relative paths
  (resolved against the pane's working directory) become `/api/panes/<pane>/files/raw?path=…` and open in a new tab; text,
  images and PDFs preview inline, everything else downloads as an attachment (the response carries a `sandbox` CSP so a
  file can never run script in the console). Percent-encoded (non-ASCII) hrefs are decoded first and a `file.md:3685`
  line suffix is stripped; http(s), mailto and anchor links are left alone.
- clicking a `.md/.markdown/.mdx/.txt/.log/.json/.yaml/.csv` link opens an in-console reader instead of a new tab: markdown uses
  the same renderer (headings, tables, code blocks, mermaid, block controls), `源码` toggles the raw text, `新标签打开` opens the
  raw file, Esc or a backdrop click closes it; relative links and images inside the document resolve against its own folder, and a
  nested markdown link opens in the same panel. Files above 1.2 MB start in source mode with a `渲染` switch.
- sends are shown optimistically (发送中/已发送) and reconciled when the transcript echoes them: text is compared whitespace- and
  wrapping-insensitively, and a rewritten/expanded echo is paired by order, so a placeholder never lingers after later replies;
  the placeholder sits right after the last message that existed when send was pressed, not at the very end
- Stopping a turn: while the agent works and the composer is empty the send button becomes a red stop button (square icon);
  clicking it (or pressing Esc in the composer) sends the key that CLI expects - `Ctrl+Q` for Hermes (Ctrl+C also interrupts
  but a second press within two seconds force-exits its TUI, so it is never sent automatically) and `Esc` for Claude Code,
  Codex and Kimi. `POST /api/panes/{pane}/interrupt` picks the key; the Flutter client has the same stop button. Typing text turns it back into
  the send arrow, and Enter always sends. Backed by `POST /api/panes/{pane}/key` (Escape/Enter/Tab/C-c/C-d/C-u/arrows only).
- Reasoning strip: a small line pinned right above the composer reads `…thinking… 1m38s` (animated dots plus elapsed) while the
  agent works and `…idle…` otherwise; the header mirrors it as `<provider> · thinking…` with a breathing dot. It is derived from the transcript (the last message is still a user message)
  plus the pane-screen probe (`esc to interrupt`/spinner, which covers streaming agents such as codex); when both settle the line
  disappears and the sidebar dot goes back to idle.
- Long conversations page backwards: the first read takes 300 messages, scrolling to the top doubles it (300→600→1200…, capped by
  `TMUX_WEB_TRANSCRIPT_LIMIT_MAX`, default 3000) and the top bar also offers 加载更早的消息; the reading position is preserved and the
  control disappears once the session start is reached. The share action in the top bar is an icon only (arrow to the upper right).
- The share dialog sizes itself with `100vw`, not `100%`, so it no longer jumps when the viewport or keyboard changes; on phones
  (≤600px) it becomes a bottom sheet: full width, rounded top corners only, 88dvh cap, the form scrolls inside and the action
  buttons stay sticky at the bottom. HTML pages are served with `Cache-Control: no-store`.
- conversation polling every two seconds with an optimistic bubble while sending; transcripts, scroll offsets and drafts are
  cached per pane, so switching back paints instantly and revalidates in the background
- **per-message "copy all"** plus **per-block controls**: copy code, copy tables as TSV, copy mermaid source, and flip tables
  between rendered and source
- share links open by slug (optionally with a password); read-only shares hide the composer and show a banner
- not included: terminal mode, the file browser, session creation, and mermaid rendering (use the web UI for those)

```bash
cd client
flutter pub get
flutter test                 # 44 unit / widget tests
flutter build apk --release --split-per-abi
```

iOS is built by GitHub Actions on `macos-latest` (`.github/workflows/mobile.yml`): unsigned IPA by default, signed and
uploaded to TestFlight once the Apple credentials are configured. See `client/README.md`.

## Share a conversation

Create a share from any pane (agent transcript, or a shell pane rendered as screen text) and manage it at `/shares`:

- expiration
- optional password
- extension
- password reset/removal
- immediate stop
- permanent deletion

Creating a share has an "allow the guest to keep chatting" checkbox (on by default). Unchecked makes it a **read-only share**: the page still shows the history and live updates, but the composer is replaced by a note and `POST /share/<slug>/api/input` answers 403, so nothing can be typed into that pane. The `/shares` list marks each share as interactive or read-only.

Guests only get the bound conversation (plus the right to send messages when enabled). They cannot access the host tree, terminal mode, or administration APIs.

## Optional MCP mailbox

```bash
chmod +x install.sh mailbox.py mcp_server.py
./install.sh
```

This installs a fixed-identity `hermes-mailbox` MCP service for Claude Code and Codex with three tools:

- `send_message`
- `receive_messages`
- `reply_message`

Local example:

```bash
./mailbox.py send claude 'Review the current design' --topic review
./mailbox.py inbox
```

## Cross-server relay MCP

TmuxWeb exposes an MCP endpoint (`POST /mcp`, Streamable HTTP), so a client only needs a URL and a token — no python3,
no copy of this repo, and upgrades happen server-side:

```
URL:     http://<host>:8787/mcp
Header:  Authorization: Bearer <TMUX_WEB_API_TOKEN>
```

Tools: `list_agents`, `ask_agent`, `send_agent`, `read_agent` (screen text, or `transcript=true`).

```bash
./install_relay_mcp.sh --dry-run   # preview, token masked
./install_relay_mcp.sh             # HTTP mode for claude / codex / kimi
./install_relay_mcp.sh --stdio     # legacy: clients spawn `python3 mcp_relay.py`
./install_relay_mcp.sh --uninstall
```

For a LAN/Tailscale-only deployment set `TMUX_WEB_MCP_AUTH=off` in `.env` (default `on`): `/mcp` then needs no header and clients are configured with the URL alone. Anyone who can reach the port can drive panes in that mode — the startup log warns once — while `/api/*` and the web login keep their checks.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

See [TESTING_EN.md](TESTING_EN.md) for automated, real tmux, remote SSH, mobile, and public reverse-proxy acceptance procedures.

## Behind Cloudflare / a reverse proxy

Share links need **no** domain configuration: `/api/shares` returns a relative URL (`/share/<slug>`) and the share page
only makes relative requests, so the browser uses whatever host you opened it with.

Two things are worth setting once you put a proxy in front of it:

```bash
TMUX_WEB_TRUST_PROXY=1            # rate limit per real client, Secure cookies from X-Forwarded-Proto
TMUX_WEB_ALLOWED_HOSTS=chat.example.com   # optional: 127.0.0.1/localhost always allowed
```

Without `TMUX_WEB_TRUST_PROXY` every request appears to come from the proxy's IP, so the login rate limit becomes a
single shared bucket and a few failures lock everyone out for 60 seconds. WebSockets work through Cloudflare as-is, and
the MCP endpoint is simply `https://chat.example.com/mcp`.

## Data and privacy

The following must never be committed:

- `.env`
- `*.db`, `*.db-wal`, `*.db-shm`
- `.remote_hosts.key`, SSH private keys, passwords
- `tmux-panes.json`
- cookies, logs, terminal captures, and AI transcripts

Structured transcript adapters read each CLI's existing local state and do not copy transcripts into this repository.

## Known limitations

- Remote transcripts do not need `python3`. Hermes needs `~/.hermes/sessions/<id>.jsonl` or `sqlite3` (PATH, common conda locations and `bash -lc` are tried); otherwise Hermes' own python3 runtime (`~/.hermes/tools/python-*/bin/python3`) exports the rows with the stdlib `sqlite3` module, and only then does capture win.
- Relay is synchronous: the caller waits for the target agent, up to 300s, and one relay request per pane at a time.
- Relay relies on the target agent honouring the marker protocol; otherwise it times out and returns the pane tail.
- Sharing requires a stable provider session; capture-only panes cannot be shared.
- Administration currently uses one shared password.
- Third-party browser dependencies are vendored under `web/vendor/` for CDN-free operation; their notices remain in the source files.

## License

[MIT](LICENSE)
