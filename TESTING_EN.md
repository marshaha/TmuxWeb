# Testing and acceptance

This document covers automated tests that do not require real AI CLIs, plus manual acceptance for local tmux, remote SSH, mobile browsers, and public reverse proxies.

- File panel: `Ctrl/⌘+click` toggles a row without navigating or downloading, `Shift+click` selects the range from the last anchor, plain click keeps its old meaning (enter dir / download file). Checkboxes are always visible; the 全选 button flips to 取消全选 once everything is selected.
- File panel: parent navigation lives only in the `..` row (the toolbar no longer duplicates it). The toolbar download button must zip the whole current directory when nothing is selected, and only the selection otherwise — its tooltip follows the count.
- File panel: type `/etc` in the path bar and press Enter — the listing must switch; relative values (`..`, `src`) must resolve against the pane cwd, Esc restores. Tick "多选", select two files plus one directory, download the zip: entry names must be relative to the current directory with the directory contents intact. Repeat on a remote pane (only `tar` is required there), and confirm an oversized selection returns an explicit 503 instead of hanging.

- Remote host form submit: after typing a password and pressing 测试联通, the `POST /api/remote-hosts/test` request body must carry `"credential":"<typed value>"` with the matching `auth_type`. Checking that the field is merely visible is not enough — a `fields.credential.value` mistake once sent `undefined` and every save/test answered 422.
- Remote host form: the password is masked by default and only the eye button reveals it; there is no fingerprint confirmation checkbox — testing or adding the host scans and pins the key automatically, the fingerprint is shown as a muted line under the form, and a later key change is refused.

- Agent mode: after adding the local `~/.ssh/id_*.pub` to the remote `authorized_keys`, choosing "本机密钥（SSH Agent）" with no credential must test successfully and list panes; the stored host has `auth_type=agent` and no `credential`. Without authorisation it must fail fast (`Permission denied (publickey,password)`) instead of waiting on a prompt.

- New session: the "+" on a host row opens a dialog prefilled with that host's first pane path; creating must add the session on that host and select the new pane. A duplicate name returns 409, an invalid name or a missing directory returns 422, and the same works on a remote host (pane id carries the host prefix).

- Relay MCP: `TMUXWEB_URL=... TMUXWEB_TOKEN=... python3 mcp_relay.py --selftest` must list every agent pane including remote `host~%pane`; drive it over stdio (`initialize`, `tools/call list_agents`, `ask_agent` against a test pane) and check a busy pane yields `isError` with `pane_tail`, and `read_agent transcript=true` returns messages.
- HTTP MCP endpoint: `POST /mcp` with `Authorization: Bearer <token>` must answer `initialize` with a protocolVersion, `tools/list` with four tools, return 202 for notifications (no id), 405 for GET and 401 without a token; `tools/call read_agent` returns pane content and missing arguments come back as `isError`, never a 500.
- No-auth mode: with `TMUX_WEB_MCP_AUTH=off` in `.env` and a restart, `POST /mcp` without an `Authorization` header must answer 200 while `/api/hosts` still answers 401, and the startup log emits one unauthenticated warning; switching back to `on` restores the 401.

- Proxy/Cloudflare: with `TMUX_WEB_TRUST_PROXY=1`, a login request carrying `CF-Connecting-IP` / `X-Forwarded-Proto: https` must return a `Secure` cookie; with `TMUX_WEB_ALLOWED_HOSTS=chat.example.com` any other `Host` answers 421 while that host and `127.0.0.1` work.

- Chat blocks: a message with python code + mermaid + a table must yield three `.block-wrap`s; copying a block flips the button to 已复制, mermaid renders to SVG, and the table toggles between rendered and markdown source. The message-level 复制全部 sits inside the card above a rule. Code containing `<` `>` `&` must render verbatim (a pre-escape + marked double-escape once turned it into `&gt;`), and a `<script>` in a message must stay text (`window.__pwned` unset).

- Read-only share: create a share with "allow the guest to keep chatting" unchecked; `/share/<slug>` must show the 只读 badge and a read-only notice instead of the composer, `POST /share/<slug>/api/input` must return 403 (while `/api/transcript` stays 200), and a share created with the box checked must still accept input. An old `shares.db` must gain the `allow_input` column on restart, defaulting to 1 so existing shares stay interactive.

- Flutter transcript cache: `flutter test` covers 'a cached conversation paints before the network answers' (first frame renders
  cached messages with a 更新中… badge, replaced once the fetch lands), 'the draft is kept per conversation', and 'the cache keeps a
  bounded working set'. On device: open pane A, open pane B, return to A - A's messages appear immediately (brief 更新中… badge)
  and its scroll offset plus unsent draft are unchanged.
- Native mobile client: `cd client && flutter test` passes (block splitting order and escaping, table parsing/TSV, URL normalization, cookie login, pane-ref encoding, read-only share 403, chat rendering and copy, block source toggle, composer hidden when read-only, send success/failure, login validation and remembered URL, token login). `flutter analyze` is clean. Device smoke test: `http://<lan-ip>:8899` + password → host tree → open `%7` → copy a message, toggle a table to source, and open a read-only share link and confirm there is no composer. Build check: `flutter build apk --release --split-per-abi` and `appbundle` succeed and `apksigner verify --print-certs` reports `CN=TmuxWeb`.

- Incremental remote transcripts: `python -m unittest test_remote_hosts -k incremental` (a second poll sends only the byte offset, a rewritten file refetches the window, a vanished file falls back to discovery) and `-k gzip` (gzip payloads decode, broken payloads raise). Measured against a host with a 300 MB transcript: first transcript call ~8s, later polls 1-2s, bodies <=60KB.

- Pane-switch cache: open a pane with a transcript and a plain shell pane, then switch back and forth - the visited pane must
  repaint immediately (status flips from 更新中… back to `xxx transcript`), and scroll offset plus the composer draft are kept
  per pane (a shell pane must not inherit the other pane's draft). `tests/test_tmux_web.py` asserts the page contains
  `paintCachedTranscript`, `rememberTranscript` and `state.drafts`.

- Host editing: on `/hosts`, rename/host-edit a saved host and save - the list updates and the stored password still works when the
  credential field is left empty; changing the hostname rescans the fingerprint; up/down reordering survives a reload and
  matches `/api/hosts`; switching to password auth without a password returns 422; saving an unreachable configuration returns
  424 and the stored fields roll back. Covered by `python -m unittest test_tmux_web -k remote_host`.

- Remote session rollover: end an agent session and start a new one in the same remote pane (the title changes); the UI must switch
  to the new session within `TMUX_WEB_TRANSCRIPT_RELOCATE` (30s by default), and the refresh button must force a lookup
  immediately (`refresh=1`). Unit tests cover title-change relocation, the relocation interval and `refresh=True` dropping the
  incremental state.

- File links: a message containing `[label](/abs/path/file.png)` or `![](relative.png)` must open the file in a new tab with
  images rendered inline, keep `https://` links untouched, and answer 404 for missing paths. Unit tests:
  `test_raw_endpoint_serves_text_inline_for_preview`, `test_raw_endpoint_requires_auth`,
  `test_rendered_transcript_links_are_rewritten_to_the_raw_endpoint`, `test_markdown_links_open_an_in_console_preview`. Clicking a
  `.md` link opens the in-console reader (headings, tables, code blocks), `源码`/`渲染` toggle, Esc or backdrop closes, `新标签打开`
  opens the raw file, and relative links inside the document resolve against its folder. `.json`/`.log` show as monospace source.

- Optimistic send: a bubble labelled 发送中/已发送 ✓ appears immediately; once the transcript echoes it the placeholder is retired and
  a single real message remains. If the agent replies before the echo (or the echo was rewritten), the bubble must keep its
  send-time position instead of drifting to the bottom. Covered by `node --test tests/js/reconcile.test.mjs`.

- Back-paging: open a session with several hundred messages - 300 are shown, 加载更早的消息 appears at the top, scrolling up loads the
  next page (300→600→1200) without moving the reading position, and the control disappears at the session start. `has_more` is true
  at `limit=300` and false once everything is loaded (`tests/test_transcript_reader.py::PagingTests`).

- Reasoning strip: send to an agent pane (or watch one work) - the line above the composer reads `…thinking… Xs` and counts up,
  the header shows `<provider> · thinking…` and the status dot turns orange; once the reply lands the line reads `…idle…`. Plain
  shells never show it. The API reports `waiting: true` when the last message is a user message
  (`test_transcript_reports_an_unanswered_turn`).

- Share dialog sizing: on phones (390/320px) it is a full-width bottom sheet with single-column fields; the width does not change
  when the share URL appears, long content scrolls inside, and the 取消 / 创建并复制链接 buttons stay visible even with a 480px
  tall viewport (`test_share_dialog_is_a_fixed_size_sheet_on_phones`, `test_html_pages_are_sent_without_http_cache`).

- macOS host (Homebrew tmux): adding a Mac with password auth must connect; a real failure should be actionable
  (`brew install tmux`) rather than a bare `command not found`. Covered by four unit tests around TMUX_PROBE_SCRIPT and TMUX_BIN.

- Host form: 主机/IP spans its own full-width row (also in the edit dialog); 显示凭据内容 turns the password into plain text and
  unmasks the private key; 不校验连接直接保存 must save a host with a wrong password or an unreachable address (it shows offline with a
  pending fingerprint), and the key is pinned on the first successful connection (TOFU); covered by
  `test_a_host_may_be_saved_without_a_working_credential`, `test_a_host_can_be_saved_without_a_fingerprint` and
  `test_an_unpinned_host_is_accepted_once_and_then_remembered`.

- Stop a turn: with an agent pane busy and an empty composer the send button turns into a red stop button
  (停止当前推理（Esc）); clicking it shows 正在停止… / 已发送 Esc and the pane receives Esc, interrupting the turn. Typing text
  restores the send arrow, clearing it restores stop, and Esc inside the composer also stops. Unit tests:
  `test_escape_can_be_sent_to_interrupt_a_turn`, `test_send_button_turns_into_a_stop_button_while_working`.

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

- login and API authorization (cookie plus `TMUX_WEB_API_TOKEN` header/bearer)
- tmux discovery, capture, input, and WebSockets
- PTY resize and cleanup
- Claude/Codex/Hermes/Kimi transcript parsing and bounded reverse JSONL reads
- share creation, passwords, expiration, extension, stop, and deletion (local and remote panes)
- encrypted remote-host storage, host-scoped pane IDs, and SSH argument construction
- SSH multiplexing arguments, private mux directory mode, environment opt-out
- remote digest polling (no text transfer when unchanged), remote transcript JSON and error propagation
- relay state detection, marker protocol, busy/timeout (504 with tail), cross-host forwarding, relay log
- file panel: listing order/hidden/truncation, path and file-name rejection, download headers,
  uploaded byte fidelity, upload size limit, remote helper and SSH transfer arguments
- provider/capability exposure in pane payloads
- keeping passwords out of argv and ordinary environment values
- rejecting public keys pasted as private keys
- the printable remote tmux field-separator regression
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

## 6. Remote rendering and performance acceptance

Remote panes must look and behave exactly like local ones:

1. A pane whose title contains non-ASCII text must keep it in `/api/hosts` (not `____`).
2. Selecting a remote Claude/Codex/Kimi/Hermes pane must show a structured transcript (status bar reads `<provider> transcript`), not an ANSI capture.
3. Remote transcripts must work with only sh/grep/tail on the host. For Hermes, a host with neither a session jsonl nor `sqlite3` falls back to capture without an error banner.
4. `GET /api/panes/<host_id>~%N/capture` works for remote panes the same way as local ones.
5. While a live view is open on an idle pane, only short `cksum` commands should appear — no repeated full-screen transfers:

```bash
ps -ef | grep 'ssh .*tmux capture-pane' | grep -v grep
```

6. With three remote live views open, only one SSH connection should exist (ControlMaster reuse):

```bash
ls -l "${XDG_RUNTIME_DIR:-/tmp}/tmuxweb-mux-$(id -u)/"
```

7. Restarting with `TMUX_WEB_SSH_MUX=0` must still work, with higher latency.

## 7. Cross-server relay acceptance

Start the model-free stand-in agent pane from the repository:

```bash
tmux new-session -d -s relay-test "python3 -u tests/fake_agent_pane.py"
tmux list-panes -a -F '#{session_name} #{pane_id}'
```

Then:

```bash
export TMUXWEB_URL=http://127.0.0.1:8787
export TMUXWEB_TOKEN=...            # TMUX_WEB_API_TOKEN from .env
./relay_client.py peers             # relay-test pane should be listed as idle
./relay_client.py ask %N 'what is 1+1?' --timeout 30
# => fake answer to: what is 1+1?

./relay_client.py ask '<host_id>~%N' 'ask across the SSH hop' --timeout 30
```

The first remote transcript read caches `transcript_reader.py` on the remote host at
`${XDG_RUNTIME_DIR:-/tmp}/tmuxweb-reader-<hash>.py` (mode 0600, content-hashed so upgrades replace it).

Edges to verify:

1. A generating pane (spinner or `esc to interrupt`) returns 409; `--force` sends anyway.
2. An agent that ignores the marker protocol returns 504 with the pane tail.
3. Two concurrent `ask` calls on one pane: the second returns 409.
4. `/api/relay/log` records OK/timeouts (last 100, cleared on restart).
5. Real CLI smoke test: pick an idle Claude/Codex/Hermes pane and ask something tiny; never use a pane with unsaved user input.
6. A wrong token returns 401, and token auth is disabled entirely when `TMUX_WEB_API_TOKEN` is empty.

Cleanup:

```bash
tmux kill-session -t relay-test
```

## 8. File panel acceptance

Point a throwaway pane at a test directory (never a production directory):

```bash
mkdir -p /tmp/tmuxweb-files/sub && echo hello > /tmp/tmuxweb-files/a.txt
tmux new-session -d -s filetest -c /tmp/tmuxweb-files 'bash'
```

1. Select that pane and press 「▤」: folders first, sizes shown, hidden files hidden by default.
2. Enter `sub`, come back with `..` and with the `↑` button.
3. Click `a.txt`: it downloads with the right name and content (non-ASCII names must survive).
4. Drop a file on the panel (or use 「⇧」 to pick several): it appears in the current directory with identical bytes.
5. Uploads over `TMUX_WEB_FILE_UPLOAD_MAX` return 413; names containing `/` or `..` return 422.
6. Repeat 1–4 on a remote pane to exercise the SSH transport.
7. On a remote without `python3` (or a minimal `env -i sh -c` shell), listing must still work: file management
   only needs `find`/`stat`/`head`/`cat`.
8. With the remote host offline the panel shows the error instead of a blank list; 「↻」 retries.
9. Remote transcripts follow the remote file: add a message on the remote session and it must show up within 1–2s without a page reload, while an idle remote only costs one `stat` per second.

Cleanup: `tmux kill-session -t filetest; rm -rf /tmp/tmuxweb-files`

## 9. Share lifecycle acceptance


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

## 10. Mobile/PWA acceptance

Test at least iOS Safari and Android Chrome:

- Opening the page does not autofocus the composer.
- The composer remains visible when the keyboard opens.
- Enter inserts a newline on mobile; the visible send button submits.
- IME composition does not submit partial text.
- Sidebar collapse works; wide code and Markdown tables scroll inside their bubble.
- Terminal shortcuts and directional controls work.
- Closing the keyboard refits and repaints the terminal without a black stale area.
- PWA installation works, and offline storage does not contain transcripts, API responses, or login pages.

## 11. Public reverse-proxy acceptance

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

## 12. Pre-publication privacy check

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
