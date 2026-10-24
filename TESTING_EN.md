# Testing and acceptance

This document covers automated tests that do not require real AI CLIs, plus manual acceptance for local tmux, remote SSH, mobile browsers, and public reverse proxies.

## 1. Install test dependencies

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt httpx
```

`httpx` is only needed by FastAPI's `TestClient` tests.

## 2. Automated tests

```bash
python -m py_compile *.py tests/*.py
python -m unittest discover -s tests -v
```

The suite uses fake backends and temporary databases. It does not connect to real SSH hosts or type into real tmux/AI sessions. Coverage includes:

- login and API authorization
- tmux discovery, capture, input, and WebSockets
- PTY resize and cleanup
- Claude transcripts and bounded reverse JSONL reads
- share creation, passwords, expiration, extension, stop, and deletion
- encrypted remote-host storage, host-scoped pane IDs, and SSH argument construction
- keeping passwords out of argv and ordinary environment values
- rejecting public keys pasted as private keys
- the real-tab remote tmux parser regression
- MCP mailbox protocol and identity restrictions

GitHub Actions runs the same compile and unittest commands on Python 3.11, 3.12, and 3.13.

## 3. Local tmux smoke test

Create a disposable pane:

```bash
tmux new-session -d -s tmuxweb-smoke 'python3 -q'
tmux list-panes -t tmuxweb-smoke -F '#{pane_id} #{session_name}:#{window_index}.#{pane_index} #{pane_current_command}'
```

After starting TmuxWeb, verify:

1. Correct login succeeds and an incorrect password returns 401.
2. The tree contains `localhost → tmuxweb-smoke → window → pane`.
3. Chat/capture mode shows the Python prompt.
4. With submit disabled, send `print("PASTE_ONLY")` and verify it is pasted but not executed.
5. Enable submit, send `print("SUBMIT_OK")`, and verify the output.
6. Terminal mode accepts input and resize events.
7. Close the page and verify the original tmux session still exists.

Clean up:

```bash
tmux kill-session -t tmuxweb-smoke
```

Never perform input smoke tests in a user's active AI pane.

## 4. Structured transcript acceptance

Select active Claude Code, Codex, Hermes, and Kimi Code panes:

- Claude: verify user/assistant order, Markdown tables/code, and omission of compact-summary internals.
- Codex: verify pane title/thread resolution to the expected rollout.
- Hermes: verify pane cwd resolution to the latest active CLI session.
- Kimi Code: verify pane cwd + title resolution to `state.json`/`wire.jsonl`, with thinking, tool output, and internal notifications filtered out.
- Unknown providers: verify ANSI capture fallback instead of guessed transcript data.

For large JSONL transcripts, run concurrent requests while monitoring RSS. Memory must scale with the response limit, not the source file size.

## 5. Remote SSH/tmux acceptance

Use a dedicated test account rather than production root credentials.

1. Open `/hosts`.
2. Scan the host key and verify the SHA256 fingerprint through a trusted channel.
3. Confirm it and run the connection test.
4. Verify failed tests do not save a host.
5. Correct credentials should report pane and session counts.
6. Save and verify the remote pane appears in the tree.
7. Test capture and literal input delivery.
8. Change the server host key and verify the connection is rejected.
9. Delete the host and verify list APIs no longer return it.

If interactive SSH sees tmux but the UI reports zero panes, test the same user in a non-interactive command:

```bash
ssh user@example-host 'id -un; command -v tmux; tmux list-sessions; tmux list-panes -a -F "#{pane_id}|#{session_name}|#{window_index}|#{pane_index}|#{pane_current_command}"; printf "PATH=%s\nTMUX_TMPDIR=%s\n" "$PATH" "$TMUX_TMPDIR"'
```

The tmux socket must belong to the SSH user; different users normally see different tmux servers.

## 6. Share lifecycle acceptance

Use a disposable local Claude session:

1. Create a short-lived password-protected share.
2. Open it in a private browser without administrator cookies.
3. Verify wrong password returns 401 and the correct password unlocks the transcript.
4. Verify guests cannot access `/api/hosts`, `/hosts`, or `/shares`.
5. Reset the password and verify the old guest cookie stops working immediately.
6. Extend the share and verify the expiration increases.
7. Stop it and require 410 from guest APIs.
8. Delete it and require 404.

Do not send acceptance prompts into a user's real conversation.

## 7. Mobile/PWA acceptance

Test at least iOS Safari and Android Chrome:

- Opening the page does not autofocus the composer.
- The composer remains visible when the keyboard opens.
- Enter inserts a newline on mobile; the visible send button submits.
- IME composition does not submit partial text.
- Sidebar collapse works; wide code and Markdown tables scroll inside their bubble.
- Terminal shortcuts and directional controls work.
- Closing the keyboard refits and repaints the terminal without a black stale area.
- PWA installation works, and offline storage does not contain transcripts, API responses, or login pages.

## 8. Public reverse-proxy acceptance

At the HTTPS endpoint:

```bash
curl -I https://your-host.example/login
```

Verify:

- `/login` is reachable and protected pages redirect or return 401 before login.
- APIs, WebSockets, and management assets are authenticated.
- SSH/tmux dependency errors retain JSON detail instead of becoming a generic proxy 502.
- The Service Worker does not cache sensitive requests.
- Monitoring reports the origin clearly when the local service is stopped.

## 9. Pre-publication privacy check

```bash
git status --ignored --short
git ls-files

git grep -nE 'BEGIN (OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY|password\s*=|api[_-]?key\s*=|token\s*=' -- . ':!tests/**' || true
```

Confirm Git does not track:

- `.env`
- databases and WAL/SHM files
- SSH private keys or `.remote_hosts.key`
- cookies, logs, and captures
- real public IPs, domains, or usernames
- Claude/Codex/Hermes transcripts
