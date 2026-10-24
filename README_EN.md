# TmuxWeb

[中文](README.md) · [Testing](TESTING_EN.md) · [测试与验收](TESTING.md)

TmuxWeb is a self-hosted control center for local and remote tmux sessions and AI CLI conversations. It organizes panes as `host → session → window → pane` and provides a ChatGPT-style conversation view, a real PTY terminal, structured transcripts, and controlled session sharing.

> Security notice: TmuxWeb can type into tmux panes and expose an interactive terminal. This is equivalent to access as the operating-system account running the service. Do not expose it directly to the Internet. Use HTTPS, access controls, a strong password, and a firewall.

## Features

- **Multi-host control plane**: one local service manages localhost and SSH hosts; remote machines only need SSH and tmux.
- **Tmux tree**: host/session/window/pane navigation with persisted expansion state.
- **AI chat view**: structured transcripts for Claude Code, Codex, Hermes, and Kimi Code, with ANSI `capture-pane` fallback.
- **Real terminal**: xterm.js over a PTY with input, resize, ANSI, and mobile shortcut controls.
- **Literal input delivery**: messages use tmux buffers instead of shell interpolation and account for stale drafts and bracketed paste.
- **Remote SSH**: password or private-key authentication, pinned host-key fingerprints, and Fernet-encrypted credentials at rest.
- **Conversation sharing**: slug, password, expiration, extension, password reset, stop, and delete controls.
- **Mobile/PWA support**: visual viewport and safe-area handling; sensitive API and transcript data are never Service Worker cached.
- **Optional MCP mailbox**: controlled text messaging among Claude Code, Codex, and Hermes without arbitrary shell or filesystem tools.

## Design boundaries

- Chat mode never keeps a `tmux attach` client alive, avoiding terminal-size interference between devices.
- Only terminal mode attaches through a PTY; the attachment is released after WebSocket disconnect.
- Remote panes fall back to SSH `capture-pane` when a structured remote transcript is unavailable.
- Remote pane references are host-scoped so `%0` from different tmux servers cannot collide.
- The current administration model is single-user. Multi-user deployments still require per-user host authorization and auditing.

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
3. Scan and verify the SHA256 host-key fingerprint through a trusted channel.
4. Confirm the fingerprint.
5. Test SSH and remote `tmux list-panes` without saving.
6. Save only after the test succeeds.

Passwords and private keys are not returned by list APIs. A private key must be a complete OpenSSH/PEM private key, not a one-line `ssh-ed25519 AAAA...` public key. Passphrase-encrypted private keys are not currently supported.

## Share a conversation

Create a share from a structured local Claude conversation and manage it at `/shares`:

- expiration
- optional password
- extension
- password reset/removal
- immediate stop
- permanent deletion

Guests can only read and continue the bound conversation. They cannot access the host tree, terminal mode, or administration APIs.

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

## Tests

```bash
python3 -m unittest discover -s tests -v
```

See [TESTING_EN.md](TESTING_EN.md) for automated, real tmux, remote SSH, mobile, and public reverse-proxy acceptance procedures.

## Data and privacy

The following must never be committed:

- `.env`
- `*.db`, `*.db-wal`, `*.db-shm`
- `.remote_hosts.key`, SSH private keys, passwords
- `tmux-panes.json`
- cookies, logs, terminal captures, and AI transcripts

Structured transcript adapters read each CLI's existing local state and do not copy transcripts into this repository.

## Known limitations

- Remote panes currently use capture/input; structured remote transcript adapters are not implemented yet.
- Sharing requires a stable local provider session and should not be enabled for capture-only remote panes.
- Administration currently uses one shared password.
- Third-party browser dependencies are vendored under `web/vendor/` for CDN-free operation; their notices remain in the source files.

## License

[MIT](LICENSE)
