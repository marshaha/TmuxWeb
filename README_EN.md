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
- **One pane, one name (fully two-way)**: pins and home favourites stay separate lists but share the name.
  Renaming in the sidebar writes the card, renaming on the board writes the pin, pinning/starring without a
  name inherits the other side's name, and clearing it resets both to the generated one. Opening an older
  `home.db` merges names that had drifted apart (only empty sides are filled, nothing is deleted). Display
  order is fixed at pin label → card title → `session: window`, used by the sidebar, the console title bar
  (and browser window title), every `/grid` cell and `/preview`.
- **One short number per window (`#N`)**: shared across hosts (`pane_numbers` in `home.db`, assigned on first sight,
  never reused), shown in the session tree, in 最近, in the console title bar, in every `/grid` cell header, and in the `/preview` and `/term` headers. The browser/PWA window title carries the same `#N` prefix (console, a single or maximised grid window, preview, terminal). It is the handle for `?pane=#12`, for the MCP relay
  (`ask_agent` / `send_agent` / `read_agent` accept `target: "#12"`, and `list_agents` returns `handle`), and for talking
  about a specific agent in prose. Resolution lives in `pane_or_404` / `pane_transport`, so `read_agent`, `send_agent`, `ask_agent` and any `/api/panes/#12/...` HTTP call accept the short form. A number whose window is gone answers 404 with an explanation instead of resolving to
  somebody else's session.
- **Sidebar hover preview**: rest the pointer on a session-tree row or a 最近 row (2s by default, 1/2/3s or off
  in the settings panel) and the last message floats next to it, with 完整预览 / 聊天 / 关闭 at the bottom.
  It is the *same* implementation the board cards use (`/assets/pane-preview.js` + `/api/home` data); a pane that is
  not a favourite or recent entry fetches its transcript once on hover (reused for 60s). Mobile web has no hover, so
  it is off by default; `?preview=1` forces it on for browsers that misreport hover support.
- **File panel: rename and delete**: pick one entry and the toolbar shows 重命名 (renames inside the same directory,
  never overwrites); pick one or more and it shows 删除 (a non-empty directory needs the "remove its contents too"
  checkbox). Desktop can right-click and a phone can long-press (520 ms) for the same menu
  (open / download / rename / delete); `F2` renames and `Delete` deletes from the keyboard. Backed by
  `POST /api/panes/<pane>/files/rename` and `/files/delete`, which refuse `/`, `$HOME` and top-level directories.
- **Wide tables cannot blow up a bubble**: markdown tables live in a `.table-holder` that scrolls sideways (keeping
  natural column widths instead of squeezing every cell into one character per line), and the bubble itself is
  `max-width:min(92%,820px)` + `overflow-x:clip`, so wide content can never widen it. The per-message
  「复制全部 / time」 row stays inside the bubble (`.assistant-body > .msg-actions`) and is always visible on touch
  (`@media (hover:none)`) instead of floating at 0.45 opacity.
- **Message actions**: every message has a "复制全部" (copy all) button; each block inside a message (code / mermaid / table) has its own toolbar — copy the block (tables copy as TSV, mermaid copies the source) and, for tables and mermaid, flip between source and rendered. Mermaid is vendored locally and loaded lazily, only when a message actually contains a mermaid block.
- **Multi-window** (`/grid`): same shell as the board — the session tree sidebar is the board's (same icons,
  expand/collapse-all, draggable width, collapsible, drawer on a phone), and clicking a session in it adds that pane
  as a window (rows already on screen carry an accent mark, and clicking one just jumps to that window). Several conversations side by side on one page. Three layouts — **side by side** (draggable widths),
  **grid** and **canvas**. For the first two the shape is picked the way Word inserts a table: the grid button opens
  a 6×6 swatch, hovering previews columns × rows and says how many windows that makes, which rows they need and how
  many slots would stay empty, and one click builds it. **「自定义…」 takes any columns × rows from 1–6 each**
  (Enter applies it), and the last custom shape stays on the button (e.g. `4×2`), remembered with the layout. Then drag any
  seam to resize that one cell, with the rest of its row/column re-fitting proportionally (170 px floor); dragging a
  window's title bar reorders it; layout, ratios, order and widths live in localStorage. **Canvas** treats the whole
  panel as one canvas: every window is a free rectangle — drag its title bar to move it, drag any edge or corner to
  resize (240×160 floor). When an edge or centre comes close to another window's, a dashed guide fades in **after a
  short pause** (`SNAP=6px`, `GUIDE_DELAY=140ms`) and the window snaps to it, so a fast drag never flickers; the
  guide disappears on release and a live size badge follows the pointer. Dragging past the right/bottom edge grows the
  canvas. 「整理」 re-tiles a messy canvas, and a focused window nudges with `Alt/⌘ + arrows` (8 px, 40 px with
  Shift). Positions, sizes and the shape are all stored in localStorage. Changing the shape,
  adding or removing a window reuses the existing cells — the iframes are never rebuilt, so a running terminal is
  not dropped and a chat does not refetch its transcript. A window
  scrolled out of view drops to one request every 15s (and refreshes the moment it comes back), so four or five remote
  conversations do not saturate anything. The console sidebar's «+» pushes the current pane straight in (`/grid?add=…`), and the full preview page has a
  grid button of its own. Each window is **chat or a real terminal** (the second header button — a shell without a
  transcript starts as a terminal), switching reloads only that window; double-clicking a title bar **maximises** one
  window (Escape restores it, and it is a class toggle so a live terminal is never reconnected); **presets** name a whole
  arrangement and bring it back later. Every window is its own widget (title, status dot,
  open-in-console, remove) whose body is the embedded conversation page (`/preview?pane=…&embed=1`) — you can keep typing into it.
  The separators between windows are draggable; widths are stored per pane in localStorage, and a newly added window takes a share of
  the leftover space. On a phone the windows stack full width. Linked from the console and board sidebars. Every page
  (`/`, `/home`, `/preview`, `/grid`, `/hosts`, `/shares`) now requires a login and remembers where to return.
- **Real terminal**: xterm.js + PTY attach with input, resize and ANSI. On a phone it now works the way Termius does:
  no visible input box (an invisible contenteditable captures the soft keyboard, tapping the terminal opens it, the keyboard
  button hides it; it is contenteditable rather than an `<input>` because iOS hangs its "↑ ↓ Done" form-assistant bar off
  form controls and that bar cannot be removed from a web page — `?kb=input` switches back if an iOS build misbehaves. On iOS the terminal types on a **self-drawn keyboard** (qwerty rows, a `123`/symbols layer, del with auto-repeat,
  space, ret, and a `系统键盘` key that hands over to the system keyboard and remembers that choice), because the
  `WKFormAccessoryView` strip cannot be removed for *any* focused editable element;
  because WKWebView attaches a native `WKFormAccessoryView` (the "↑ ↓ Done" strip) to any focused editable element and
  no web API removes it; `?kb=auto` restores tap-to-type;
  button hides it); one scrollable key row (`shift tab`, paste, `?`, `/`, `esc`, `tab`, `ctrl`, `alt`, arrows, `^C`) with
  **more** and **keyboard** pinned on the right; `ctrl`/`alt` latch onto the next character; a four-column extended pad
  (`^C ^D ^Z ^L …`, `| ~ - = : ; ! $ % ^ < > ( ) { } [ ]`, home/end/pgUp/pgDn, arrows, paste/del/@/&) that replaces the
  system keyboard instead of squeezing the terminal; pinch to change the font size (9–22px, re-fitting the columns) and a
  “back to bottom” pill when you scroll away. The keyboard only ever shrinks the terminal, and the PTY gets the new size.
  `/term` (the grid's terminal cell) supports paste and pinch as well.: xterm.js over a PTY with input, resize, ANSI, and mobile shortcut controls.
- **Literal input delivery**: messages use tmux buffers instead of shell interpolation and account for stale drafts and bracketed paste.
- **New sessions**: the "+" next to a host row runs `tmux new-session -d` on that host (local or SSH), with a
  dialog for the session name (letters, digits, `.`, `-`, `_`, 1–64 chars) and an optional working directory; the
  new pane is expanded and selected afterwards, and a missing directory is rejected up front.
- **Remote SSH**: password, private-key, or "local keys / SSH Agent" authentication (no credential at all when the remote already authorises your public key), pinned host-key fingerprints, and Fernet-encrypted credentials at rest.
- **Conversation sharing**: slug, password, expiration, extension, password reset, stop, and delete controls, plus a **read-only vs. interactive** choice. **Any pane** can be shared: agent sessions render as messages, plain shell panes render as screen text and still accept input.
- On a phone the grid is always one column, one window per screen: the shell is exactly one viewport tall and the
  window list scrolls. Because the iframes inside swallow vertical drags, a small `↑ 1/2 ↓` pager sits in the
  bottom-right corner too — tap to glide to the previous/next window, with the arrows greying out at the ends.
  It sits inside `env(safe-area-inset-*)` so an iPhone's rounded corner never eats it; **long-press for 380 ms to
  drag it anywhere** (the spot is remembered locally) and **double-tap the pill's blank area to reset**.
- **Mobile/PWA support**: every page (console, board, grid, terminal, hosts, shares) ships `manifest`, `apple-touch-icon`,
  `apple-mobile-web-app-*` and `theme-color`, so "add to home screen" opens a standalone window; the manifest carries
  `shortcuts` for the board and the multi-window page. Above 760px — a desktop browser — the terminal has **no key bar**, the
  whole area is terminal; the bar and the self-drawn keyboard only exist on narrow screens. Visual viewport and safe-area
  handling throughout. The Service Worker caches **static assets only** (`shell.*`, `settings.js`, icons,
  `xterm/mermaid/ansi_up/marked`) and is network-first — every deploy wins, the cache only covers a repeat
  visit or a dead connection; pages, `/api/`, `/ws/` and login state are never cached.
- **Icons**: `web/app-icon.svg` is the single source (dark rounded tile, cream `>`, terracotta block cursor); `scripts/build_icons.py` renders the 32/180/192/512 PNGs plus a maskable variant. Every page — including login — links the favicon, and the manifest declares `any` and `maskable` separately so Android's circular crop never clips the mark.
- **File panel**: the path bar is editable (absolute, or relative to the pane cwd — Enter navigates, Esc cancels, focus selects the whole path). the toolbar download button zips the current directory (or just the selection when there is one); every row carries a checkbox, `Ctrl/⌘+click` toggles one item and `Shift+click` selects a range (desktop conventions), and the footer offers 全选 / 清空 / 下载 zip (N) for files *and* whole directories can be zipped: local panes zip straight from disk, remote hosts only need `tar` (the bytes come back in one SSH call and are re-containered locally). Limits: `TMUX_WEB_ZIP_MAX` (128MB uncompressed) and `TMUX_WEB_ZIP_MAX_FILES` (1000).
- **Optional MCP mailbox**: controlled text messaging among Claude Code, Codex, and Hermes without arbitrary shell or filesystem tools.


## Web ⇄ Flutter parity gate

The web console and the Flutter client (`client/`) share one HTTP API; a feature that only lands on one side
drifts silently. `scripts/parity.py` turns that into a hard rule: it compares **server routes**, **routes the
Dart client actually calls**, **shared `web/assets/*.js` modules** and **pages**, and everything extra must be
declared in `parity.json`:

- `ported_*`: where the Dart side lives (the path must exist — delete it and the gate fails);
- `web_only_*`: web-only, with the reason;
- `pending_*`: known debt, printed in the report, not blocking.

```bash
python3 scripts/parity.py           # report, exit 1 on undeclared drift
python3 scripts/parity.py --update  # refresh the manifest, keep written reasons
```

`tests/test_parity.py` runs it inside the server suite and `tests.yml` has a dedicated `parity` job, so a new
endpoint without a decision about the app turns CI red without installing Flutter. For every new web feature:
run the script, then either port it to Dart in the same change (`ported_*`) or record `pending_*` with a reason.

Known debt today (`parity.json`): `recents.js` (the app has no standalone 最近 block yet) and
`composer-files.js` (the app's chat cannot attach files yet; the upload API is already in the client).

## Supervision and chasing a disappearing session

`tmux kill-session`, a broken `exec` chain or a stray `kill` all look the same: the whole session
vanishes, because tmux closes it the moment its pane process exits. So run the console through
`scripts/run_service.sh` instead of `exec uvicorn`:

```
tmux new-session -d -s tmuxweb -c ~/workspace/TmuxWeb './scripts/run_service.sh'
```

- the service is restarted 2 seconds after any exit, so one crash no longer means an outage;
- every exit is appended to `logs/service-exit.log` with the code: `129`=SIGHUP (session destroyed by
  kill-session), `130`=SIGINT, `137`=SIGKILL, `143`=SIGTERM, `139`=segfault. Access logs go to
  `logs/service.log`. `logs/` is not committed;
- to catch the culprit: the `session-closed` tmux hook records which session closed and when (inside a hook the format is
  `#{hook_session_name}`, not `#{session_name}`), and
  `./.venv/bin/python scripts/tmux_watch.py --seconds 1800` records tmux client commands (a
  `kill-session` client only lives a few milliseconds, which is why polling is the only way).

What the 2026-10-30 investigation ruled out: no kernel OOM, no segfault, no `systemd-oomd` kill (although
`user@1002.service` does have `ManagedOOMMemoryPressure=kill`), no apport crash report, `destroy-unattached=off`
and no tmux server restart. In other words the session was not recycled by tmux itself: the process inside the
pane was terminated by an external signal (no shutdown log ⇒ SIGKILL/SIGHUP) and tmux then closed the session.
Pinpointing the sender needs auditd (not installed); the three recorders above plus the auto-restart now make
both the outage and the blind spot go away.

### Paths in a message are clickable

A post-processing pass (`/assets/path-links.js`) turns path-looking text into links — shared by the
console and `/preview`, and usable on any subtree:

- recognises `~/x`, `/a/b`, `./x`, `../x` and relative paths with an extension (`results/out.md`);
- the kind is resolved on click via `GET /api/panes/<pane>/files/stat?path=…` (`~` expanded server-side,
  relative paths resolved against the pane's cwd), so ordinary prose never becomes a dead link:
  directory → the file panel navigates there; file → panel jumps to its folder and downloads it;
  markdown → preview with a download button; missing → a `找不到` hint.
- text nodes only: `<pre>` blocks, existing links and buttons are left alone; Tab + Enter work.
- **lazy**: nothing is scanned at render time — a message is processed when the pointer enters it,
  a finger touches it or it receives focus, and the result is marked with `data-paths`. On touch the
  freshly created link is opened by the same gesture, so one tap is enough.
- **hover card**: resting the pointer on a path for ~0.4s shows the file name, its size (or "directory")
  and 预览 / 下载 buttons. Text-like files (`log`, `txt`, `json`, `yaml`, `csv`, source) open in the
  viewer (which has its own download button) instead of downloading silently; binaries still download.
  The card is not shown on touch devices.
- **relative-path offset, then a bounded search one level deeper**: agents often print paths relative to
  *their* cwd (e.g. `ate/s3k/ca_pu_crossboard_1102.md`) while the file really sits under a subdirectory
  (`ddr_patch/ate/s3k/...`). `GET /files/stat` resolves against the pane cwd first, then its parent and
  finally home; when nothing matches it runs a **bounded suffix search** from the cwd and its parent — one
  command per host (remote `find -maxdepth 4`, pruning `.git`, `node_modules`, `.venv`, ...; local a pruned
  `os.walk`, capped at 4000 directories). A hit answers with `via:"search"` plus `relative` (its real place
  under the cwd) and the hover card says `子目录 · <relative>`. `offset` still says which base matched
  (0 = as typed) and `path` is always the absolute result. Answers are cached for 60 s per pane+base+path,
  so hovering does not pay an SSH round trip every time.
- **mobile downloads ask first**: `TmuxDownload.maybe(href, {name, meta})` in `/assets/downloads.js`
  shows a bottom sheet (name, size, cancel/download) on touch devices and downloads straight away on
  desktop. The file panel, zip export, markdown viewer and the path hover card all go through it —
  iOS Safari otherwise jumps into a system preview with no way back.

### Attaching files from the chat composer

The 「附件」 button sits in the first row **inside** the composer card, separated from the textarea by a
1px hairline. One implementation (`/assets/composer-files.js`) is shared by the console chat and by
`/preview` — the very page each multi-window cell embeds — so both behave identically, including the
`user uploaded file “…”` prefix and the same title (最近 / 收藏 name wins). Files upload into
`upload_files/` **inside that pane's current directory** — remote panes go over the same SSH channel:

- pending attachments show above the input box: images as small thumbnails, other files as name + size;
- the × on each one removes it from the message *and* deletes the copy on disk
  (`DELETE /api/panes/<pane>/files/upload`, restricted to single names inside `upload_files`);
- sending prefixes the message with `user uploaded file “a.png & b.pdf”` and a blank line, so the agent
  knows what to read;
- switching panes clears the strip; the upload limit is the file panel's `TMUX_WEB_FILE_UPLOAD_MAX` (256MB default).

### Multi-window on a phone

- The toolbar slims down to icons (add / presets / home) and the layout switch disappears: a phone is always
  one column.
- Every window is a full-height card, one per screen, with scroll snapping.
- Windows open as **chat**; a plain shell has no transcript, so its card shows the current screen (read-only,
  refreshed every 5s) plus a "switch to terminal" button instead of an empty box. A terminal opened on purpose
  through the header toggle is remembered.

### A card whose pane dropped

When the host or pane behind a favourite is unreachable the card's status dot reads 离线 (offline) and the
preview header says so; if something was read before, the last cached answer stays on screen
("离线 · 缓存 · 10 分钟前") instead of going blank. The cache lives in memory only.

### Pausing live updates

The ⏸ button in the header only stops the stream: in terminal mode the screen stays exactly as it was (no
black screen, nothing cleared), in chat mode the rendered messages just sit there, the status dot goes grey
and the label reads 已暂停实时刷新（点 ▶ 继续）. ▶ re-attaches into the same canvas: the frozen screen stays
put until the new attach actually starts sending, and only then is it cleared and repainted, so even on a
slow link you see the old frame turn straight into the current one — no black gap. Typing is dropped while paused.

### Send failures and retry

The status line under the composer is grey by default; a successful send shows a green 已发送 ✓ that clears
after ~1.4 s, a failure shows a **red** 发送失败 and stays put (with a 重发 button on the bubble). Retrying
shows 重发中…, then the same green confirmation — the stale failure text no longer lingers in the corner.

### Themes

Settings → Theme offers six palettes: **Default (warm grey, terracotta), ChatGPT (white, green), Claude
(cream, orange), GitHub (white, blue), Claude Code (dark terminal) and Night (neutral dark)**. The choice **follows the
account**: it is stored server-side (`prefs` table in `home.db`, `GET/PUT /api/prefs`), and login hands it back
in a readable `tmux_theme` cookie so the first painted frame is already right — `localStorage` only covers the
logged-out login page and offline. Every page follows it, including a plugin's own pages.

Under the hood only the CSS variables change; `:root[data-theme=…]` in `theme.css` outranks each page's own
`:root`, so a new theme is just another block of variables — the dark ones add a dozen overrides that point
hard-coded white surfaces at the variables.

### A narrow sidebar

The sidebar header is two stacked rows: the title on top, the icons on a full-width row underneath.

- **Desktop (mouse, no touch)**: icons stay 30 px and **wrap onto a second line** when the row is full
  (left aligned, even gaps) — so even a 200 px sidebar shows all of them and nothing needs sideways
  scrolling, which is unpleasant with a mouse.
- **Phone / touch**: icons are 40 px targets, the row swipes sideways when it has to, and the right edge
  carries a fade in the sidebar's own colour (only visible once an icon slides under it) — the "there is
  more" hint.

**The sidebar is one implementation**: `assets/recents.js` + `assets/recents.css` own the 「最近」 block —
markup, data, pins, favourites, renaming, height and folding — and each page mounts it once
(`TmuxShell.mount()` / `TmuxRecents.mount()`); no page carries a second copy. The only difference between pages is
what a click does: the console switches to that conversation, the board jumps to the console with it selected, and
the multi-window page adds it as a new cell (flashing the cell if it is already open). A star in the tree and a star
on a recent row are the same favourite, so either one updates the other at once. On the console the block steps
aside while the file panel is open.

**「最近」 under the session tree**: a divider, then up to six recently opened conversations (from
`/api/home/recent`, per account) showing «host · session:window», the command and a relative time (shown on hover
so the title keeps the width), with the status dot (busy / idle / offline) on the left. Clicking one switches to it
and moves it to the top.

Each row has two small buttons on the right: a **pin** that nails it to the top of the block
(`/api/home/recent/pin`, a `pins` table server-side, per account; it does not eat a slot in the 30-entry recent
cap, survives a host going offline by showing the title recorded when it was pinned, and a hairline separates the
pinned set from the ordinary recents) and a **star** that favourites it onto the board (the same favourites the
tree and the preview page use). Clicking the pin again unpins; on touch, where there is no hover, both buttons are
always visible at a 44 px row height. Opening a pinned conversation only refreshes its timestamp — it is never
dragged out of the pinned set.

**A pinned row can be renamed**: double-click its title (long-press for 480 ms on touch, or the pencil button on
that row) and edit in place — Enter saves, Esc abandons, an empty name falls back to the generated
«host · session:window».

**Pins and favourites are two independent lists** and never create or borrow from each other: starring a pane
never grows a pin, pinning a pane never copies the card's title, and the recent list only shows a pin's own name.
The single link is an explicit rename — when one pane is both pinned and starred, renaming it updates the other
side in the same transaction (`PATCH /api/home/recent/pin/{pane}` and `PATCH /api/home/favorites/{pane}`), so the
two never disagree; leave it alone and each keeps its own default name.

Pinned rows sit in a tinted rounded panel, so they read as one group against the plain recents below (the tint is
derived from the theme's accent, so dark themes get their own).

**The block's height is adjustable and defaults to two fifths of the sidebar**: drag its top edge (`row-resize`)
to resize, clamped between 15% and 75%, double-click the handle to go back to 2/5; the ratio is remembered in
localStorage. The caret at the left of the header **folds the whole block** down to its title row, and that state is
remembered too. When it outgrows its box it scrolls with the wheel or a finger (`overscroll-behavior:contain`, so
the page behind does not bounce). The header also carries a pin that pins/unpins the **currently selected**
conversation, and the × clears the ordinary recents only, leaving pins alone. The block steps aside while the file
panel is open.

**Sessions or files in the sidebar?** The console's (`/`) sidebar shows one of the two, and the rule is
**the session list is always what you start with**. Clicking the folder icon in the header (or a directory in
the panel) switches to the file browser; while it is open it follows the selected pane, and × (or the icon
again) goes back to the sessions. The choice is **not remembered across page loads** — it used to be stored in
`localStorage`, so a chat opened from the board, the grid or a link could start with 文件管理 instead of the
server list, which looked random. The board and the grid only have the session tree, no file panel.

To remember it per session, turn on **设置 → 侧栏 → 按 pane 记住**: each pane then keeps its own
last choice — session list or file panel, including the directory you were browsing — across page loads and pane
switches; a pane you never opened files for still starts on the session list. The preference lives with the
account like the theme (`/api/prefs`, key `sidebar`, values `pane` / `none`) with a mirror in
`localStorage['tmux-sidebar-memory']` for the synchronous read at load, and the per-pane views are kept in
`localStorage['tmux-files-view']` (capped at 60 panes).

Identical on the console, the board and the multi-window page (the latter two share `shell.css`).

### Message time

The row under each bubble (which used to hold only 复制全部 / copy) now starts with the message time:
`HH:MM` for today and `MM-DD HH:MM` for older messages, in a faint tabular-numeral font; the copy button
stays on the right. User bubbles, agent bubbles and the embedded `/grid` + `/preview` views all match.

### Icons

UI icons are an inline SVG sprite (`<use href="#i-...">`). The copy glyph follows `icon.png` (two stacked
rounded sheets, `#i-copy`, 1.7 stroke, `currentColor`); the share icon stays the original arrow — `#i-external`
in the console and each page's own `#i-share` in the sidebars.

## Settings panel

Bottom-left "设置" in the console (and at the bottom of the sidebar on `/grid`; the panel itself comes from
`/assets/settings.js` and is shared by both pages):

The panel takes the focus itself when it opens, so no row is ringed (Chrome, Firefox and Safari all
highlight the first control otherwise); only Tab brings up the accent-coloured ring.

- **Servers / Shares** — two rows that jump straight to `/hosts` and `/shares`, no need to hunt for the
  little sidebar icons.
- **Change the password** — click the row: current + new (6 chars or more). After the first change the password
  no longer comes from the environment; a pbkdf2 hash lives in `passwords.json` (0600, path via
  `TMUX_WEB_PASSWORD_DB`), the browser that made the change stays signed in and every other device has to log in
  again.
- **Import / export** — one row with an export/import tab inside:
  - export asks for an export password (6 chars or more) and encrypts in the browser with
    PBKDF2(SHA-256, 250k) + AES-GCM before writing; **the password never reaches the server**. It contains
    credentials, jump chains, tmux paths, layout and presets — treat it as a key file;
  - import takes the file and that password; a wrong password fails at the AES-GCM tag, existing hosts (same
    host/port/user) are skipped, jump hops are relinked by name, layout and presets replace the local ones.
- **Log out** — bottom-left of the panel. The sidebar no longer has its own 退出 button.
## Plugins

A feature can live in `plugins/<name>/` (a `plugin.toml` plus a Python module). Only first-party plugins load
by default; `TMUX_WEB_PLUGINS` turns others on, and `GET /api/plugins` reports what is installed, enabled and
broken. **The cross-server relay is one**: its four HTTP endpoints now live in `plugins/relay/`, switch the
plugin off and they are gone. A plugin can also add a sidebar button, register an `/mcp` tool and serve its own static page
(`/plugin-assets/<name>/`, login required) — each of those must be declared as `permissions = ["ui","mcp","assets"]`
in its manifest. Plugins share the server's interpreter: the declaration constrains those three outward-facing
capabilities, it is not a kernel sandbox, so run untrusted plugins inside a container. See
[`plugins/README.md`](plugins/README.md).


## Design boundaries

- Chat mode never keeps a `tmux attach` client alive, avoiding terminal-size interference between devices.
- Only terminal mode attaches through a PTY; the attachment is released after WebSocket disconnect.
- Agent detection does not trust `pane_current_command` alone: Codex/Claude are sometimes launched through a
  shell (`zsh -c 'codex …'`, a background process, a runtime wrapper), so tmux reports `zsh` and the chat view would
  stay empty. TmuxWeb then walks the descendants of `pane_pid` (the remote host ships
  `ps -A -o pid=,ppid=,args=` in the same SSH call). Window/session names that merely mention an agent are ignored.
- Remote structured transcripts need no remote `python3` either: a POSIX shell snippet locates the session file (Claude project dir + title/screen probe, Codex `session_index`, Kimi `state.json`, Hermes `--resume` session jsonl or `sqlite3`), the bytes are pulled here and parsed by the **same local parsers** — the `scp` model. When discovery fails the pane falls back to `capture-pane`. Remote transcripts are polled at the same 1s cadence: the server caches the parsed result per (host, pane, limit), serves repeats for 2s without touching the host, then uses a single `stat` probe and only re-pulls when the file changed — so a remote conversation follows the remote file live at almost no cost while idle.
- A remote **Claude / Codex** pane's session file is located from **`--resume <id>` / `--session-id <id>` /
  `codex resume <id>` in the pane's own process argv**, and only falls back to "newest file in the cwd + title/screen scoring" when there is no such
  flag. With several Claude sessions running side by side in one directory that fallback picks the neighbour's
  transcript — the chat then sits on another conversation and looks frozen. Codex additionally needs the compact
  `session_index.jsonl` (`"thread_name":"…"`, no space after the colon) to be matched — only the spaced spelling was
  grepped, so that whole index was dead.
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

Drag the sidebar's right edge to resize it (200–640px, double-click the edge to reset); the width is remembered in
`localStorage` — and the header icon row wraps (scrolling only when even one row cannot fit) so no button is ever clipped.
The console and the board keep separate widths. On phones the sidebar is a drawer with a fixed width.

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


## New session: create the directory, optionally start an agent

The `+` next to a host opens **New tmux session**; every field is optional:

- **Working directory** - created with `mkdir -p` when missing (on the remote host, by the remote
  host). If it cannot be created the error carries the path. Pass `"create_dir": false` to keep the
  old behaviour where a missing directory is a 422.
- **Start agent (optional)** - the dropdown lists only agents actually installed on *that* host;
  leaving it empty starts a plain shell. Detection is one POSIX `sh` probe (`command -v` plus
  `~/.local/bin`, `~/.kimi-code/bin`, `/opt/homebrew/bin`, ...), so the far side needs no `python3`
  and no `--version` calls (macOS has no `timeout` to bound them).
- **Agent arguments (optional)** - split with shell rules and appended verbatim (`--model o3`,
  `--dangerously-skip-permissions`). Unbalanced quoting is a 422, never silently dropped.
- **Started by absolute path** - a tmux pane's PATH is not the PATH ssh saw: on oracle `claude`
  lives in `~/.local/bin`, so a bare `claude` is `command not found` (exit 127). The session is
  therefore started with `/home/ubuntu/.local/bin/claude`, not `claude`.
- **An exiting agent does not take the session with it** - `keep_open=true` by default: the pane
  prints `[<path> exited, code N] session kept ...` and `exec $SHELL`. Pass `"keep_open": false`
  to get the old "agent exits, session ends" behaviour.

    GET  /api/hosts/{host}/agents           # {"agents":[{"name","label","path"}], ...}, cached 5 min
    GET  /api/hosts/{host}/agents?refresh=1 # re-probe (one ssh round trip per host)
    POST /api/sessions {"host","name","path","agent","agent_args","create_dir"}

    => {"session":{"session","path","created_dir","command"}, "panes":[...]}

The catalogue lives in `agent_catalog.py` (hermes / claude / codex / kimi / opencode / gemini);
adding one is a single line in `AGENTS`, and both the UI and the API pick it up.

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
- **“Screen capture” is not a terminal** — the chat fallback used `tmux capture-pane`, i.e. a text snapshot rendered into HTML,
  rebuilt from scratch every frame; a full-screen TUI makes that expensive. Terminal mode is a real xterm + PTY (incremental VT
  writes). Agent panes therefore offer a “用终端打开（xterm）” button instead of capturing.
- **The tmux path is probed once** — the resolved absolute path (e.g. `/opt/homebrew/bin/tmux` on macmini) is saved with the host
  (`remote_hosts.tmux_path`) and reused by every command *and* by the terminal-mode PTY attach; that attach was missing the path,
  which is why switching to terminal mode on macOS printed `zsh:1:1 command not found: tmux`. The hosts page shows the saved path.
- **Each host row refreshes on its own** — the sidebar (console and board) has a refresh icon per host calling
  `/api/hosts?refresh=1&host=<id>`, which re-lists that host only (measured: macmini 0.14s, oracle 0.37s) instead of probing all
  of them for a full snapshot.
- **The transfer window adapts by itself** — no setting: a discovery pull slower than 8s halves that host's tail (4MB → 1MB
  floor), a pull faster than 2.5s doubles it back. Oracle went from 14–30s to 5–7s with the same 28 messages.
- **Slow links** — a transcript pull is bounded (`TMUX_WEB_TRANSCRIPT_TIMEOUT`, 30s; the error says the network is slow rather than
  claiming the transcript is broken), the tmux path probe is bounded (`TMUX_WEB_PROBE_TIMEOUT`, 10s, then it falls back to bare
  `tmux`), and the transfer size is tunable (`TMUX_WEB_REMOTE_PAYLOAD_BYTES`). Measured on oracle (2–4s per ssh command): ~29s to
  discover a session the first time, 0.9–7.6s for the incremental polls after that.
- **An agent pane never degrades into a firehose screen** — when the transcript is empty or unreadable the chat area shows a notice
  plus a “view raw screen” button and retries; it no longer auto-falls back to streaming the TUI (a full-screen Kimi/Claude redraw
  is ~550 lines / 7000 DOM nodes twice a second, which froze Chrome). Shell panes still show their screen.
- **Kimi session choice** — several sessions can share one cwd and title; only the newest `updatedAt` is read, because the older
  ones usually end in an aborted turn with no messages.
- **Going back to the board is instant** — `/home` paints the previous snapshot from `localStorage` first (marked
  `显示缓存，正在更新…`) and swaps in fresh data when it lands; without a snapshot it shows a skeleton (stats, cards and tree
  placeholders) instead of a blank page. On the server `/api/home` waits at most 1.2s because each favourite's live half needs an
  SSH round trip: slow panes come back as placeholders (`pending`) and fill in from a 25s cache on the next poll.
- **The session tree starts collapsed** — both the console and the board remember which branches you opened instead of rendering
  everything open, and a small **expand all / collapse all** pair sits above the tree. Clicking one server opens only that server.
- **A card always previews; the panel holds the exits** — tapping any card (favourite, recent, board) opens the preview next to it
  (a bottom sheet on a phone) with three buttons: **full preview / chat / close**; tapping outside or Escape closes it too. The old
  per-card ⤢ shortcut is gone because the panel's "full preview" does the same thing.
- **A phone tap opens the conversation** — touch fires `mouseover` first, so the hover preview appeared over the card and swallowed
  the tap: cards could only ever "preview". The hover panel is now limited to devices with a real pointer
  (`(hover:hover) and (pointer:fine)`); on a phone a tap enters the console, the ⤢ button opens the full preview page, and that page
  has its own chat/terminal entries.
- **Chat/terminal switch lives in the header** — the two view buttons moved out of the sidebar into the header icon row, right next
  to the star (stop / chat / terminal / star / share / fullscreen). They are icon-only (bubble, terminal box) with `aria-pressed`, and
  fit on one row at 390px. `/preview` has the same two entries (chat / terminal), replacing the old "open in console" button that
  duplicated the chat entry.
- **Terminal input on phones** — the field above the key bar is a keyboard *proxy*, not a buffer: every character goes to the PTY and
  the field is cleared straight away. Editing therefore cannot duplicate text ("cursor left, type" used to re-send the whole line).
  Backspace sends `\x7f`, Enter `\r`, an IME commits on `compositionend`, and arrows/Tab/Esc/Home/End/Delete go to the PTY as real
  keys. Bytes typed before the socket is open are queued until it connects.
- **Modifier keys** — a phone keyboard has no Ctrl/Alt: the key bar's `Ctrl`/`Alt` act as a one-shot prefix for the next character
  (`Ctrl` then `c` sends `\x03`), and real Ctrl/Alt combos typed on an external keyboard are translated too (`Ctrl+V` still pastes). — terminal mode has its own text field above the key bar. The soft keyboard targets it: characters go to the
  PTY as typed (so the echo is immediate), backspace sends `\x7f`, Enter sends `\r`, and an IME commits on `compositionend`.
  xterm's own textarea gets `inputmode=none`, so tapping the terminal dismisses the keyboard instead of triggering a redraw.
- **Blast radius is one window** — tmux remembers sizes per *window*. Opening a pane's terminal from the phone resizes exactly that
  window; other windows of the same session, other sessions and other servers keep their size (measured with a 3-window session plus
  a second session: only the opened one went 179x51 -> 47x39).
- **No small window left behind** — when the phone is the last client, tmux *keeps* the 47x40 after it detaches, so the next viewer
  (desktop tmux, next phone, a transcript read) sees a squashed pane. The size is captured before the attach and restored after the
  last client leaves, touching only that window: `resize-window -x/-y` writes the size back (which also flips `window-size` to
  `manual`) and the window's `window-size` mode is put back to what it was (usually `latest`, so it follows clients again). No
  session-level `default-size` is touched, so other windows cannot be affected. Remotely the attach records the size and mode in
  `/tmp/.tmuxweb-geom-<pane>` and the server restores over a fresh connection after the client is gone.
- **A PTY resize must be signalled** — the terminal pty comes from `openpty()` and the child only inherits the fds (and
  `start_new_session` leaves it without a controlling terminal), so the kernel never delivers `SIGWINCH` after `TIOCSWINSZ`.
  `resize` used to set the size only: tmux kept its old size and never repainted, which on a phone meant the terminal showed
  the few rows it already had (or just the status bar) after the keyboard closed. It now sends `SIGWINCH` to the child explicitly
  and tmux repaints at the new size. Resizing the desktop window was broken the same way.
- **No page zoom when a field is focused** — iOS zooms the whole page for an input under 16px and does not zoom back out on blur (it looks
  like "the window grew and the terminal never recovered"). Every `input`/`textarea`/`select` is 16px at the phone breakpoint: console
  composer, terminal field, board search, host admin, share and session dialogs.
- **The terminal only shrinks when the keyboard opens** — terminal mode keeps its header while composing (the old composing rules gave
  the terminal an extra 58px, which read as "the window got bigger"), and blur restores the visual height *and* refits, so the grid
  comes back to its original rows/cols.
- **No more "re-render" when the keyboard opens** — a `visualViewport` change fires continuously during the keyboard animation and the
  old code fitted plus `xterm.refresh(0, rows-1)` on each one, sending a PTY resize even when cols/rows were unchanged (tmux then
  repaints the whole screen). Now the layout follows the visual viewport immediately (`--visual-height` / `--visual-top`, i.e. it lifts),
  the fit only runs when cols/rows actually changed, the ResizeObserver is coalesced to 90ms, and a reader scrolled into the scrollback
  keeps their place. Measured: one `resize` (`rows 41→20`) per keyboard transition.
- **Login keeps deep links** — an unauthenticated `/?pane=...` redirects to `?next=` and comes back after signing in (only same-origin
  paths are accepted; `//evil` falls back to `/`).
- **Landing** — `/` redirects to `/home` when there is neither a `?pane=` nor a remembered pane; with a remembered pane the
  console restores it, so refreshing the console does not eject you.
- **Recent visits** — the board lists the four panes you opened last (one click back to the console). Visits are recorded by the
  console (`POST /api/home/recent`); titles/hosts come from the `/api/hosts` snapshot and status from the sweep, so the section
  never reads a transcript itself.
- **How busy/idle is decided** — the first source with an answer wins (the API reports it as `status_source`):
  1. **the agent's own session file** (`session`, most reliable): every agent writes an explicit record when a turn ends, so one record
     from the last 64KB is enough — Claude `{"type":"system","subtype":"turn_duration"}`, Codex `event_msg`/`task_complete`
     (`task_started` = running), Kimi `{"type":"prompt.completed"}`. The newest *decisive* record wins, so bookkeeping codex appends
     after the turn cannot hide the verdict, and a file untouched for `TMUX_WEB_STATUS_STALE_SECONDS` (600s) is not counted as busy
     (an aborted session ends on `agent.message.appended` and would otherwise look busy forever).
  2. **tmux output activity** (`activity`): `#{window_activity}` within `TMUX_WEB_ACTIVITY_BUSY_SECONDS` (3s) means output is flowing;
     it costs nothing extra, it rides along with list-panes.
  3. **screen text** (`screen`, last resort): interrupt hints / spinners / progress words with an ellipsis mean busy, a prompt or
     agent input box means idle, anything else is unknown (grey dot).
  Measured before/after: three Kimi panes were reported busy because their footer says `Kimi K3 thinking`; all three are idle now.
  Remote hosts answer with **one ssh per host** (the same rules in shell, no python3 needed there), hosts probed in parallel: 72 panes in 4.5s.
- **Star in the conversation top bar** — favourites the pane you have open; it stays in sync with the star in the sidebar tree and
  with the board cards. The conversation preview page (`/preview`) carries the same button.
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

Tools: `list_agents`, `ask_agent`, `send_agent` (`submit=false` types without pressing Enter),
`create_session` (any host, optionally with `agent` / `agent_args`, returns the new `#N`),
`kill_session` (by session name, `host` accepts an id or a name), `send_file`,
`read_agent` (screen text, or `transcript=true`).

**Enter is checked, not guessed.** Text goes in through `paste-buffer` and `C-m` is sent separately.
With a large prompt a TUI is still digesting the paste and eats the first Enter - the classic "the
text is in the box but nothing was submitted". The console now presses Enter, waits 0.45s,
compares the screen, and presses again while the screen is frozen (up to 3 times; an extra Enter on
an empty prompt is a no-op). The initial wait scales with the text length (0.12s + 1 char / 2500,
capped at 1.8s) and gets a little longer over ssh.

**`send_file` moves bytes across hosts** by relaying them through the console into the target pane's
`upload_files/`, then typing `[file] <absolute path> (N bytes)` into that pane. Neither machine needs
a shared mount or ssh trust between them.

```json
{"tool": "send_file", "arguments": {"target": "#125", "path": "/home/me/report.md",
                                    "note": "read this first", "notify": true}}
```

Limit 64MB by default (`TMUXWEB_SEND_FILE_MAX`); the destination name cannot contain path
separators; `notify=false` writes the file without interrupting the agent.

```bash
./install_relay_mcp.sh --dry-run   # preview, token masked
./install_relay_mcp.sh             # HTTP mode for claude / codex / kimi
./install_relay_mcp.sh --stdio     # legacy: clients spawn `python3 mcp_relay.py`
./install_relay_mcp.sh --uninstall
```

**Several machines or several MCP servers? Put a gateway in front.** This host runs
[MCPHub](https://github.com/samanhappy/mcphub) in `~/mcphub` on port 3300 with the TmuxWeb relay as one upstream.
Each agent configures `http://<host>:3300/mcp` once; adding another MCP server (fetch, a database, an internal
service) happens in the hub and no client changes — `/mcp/<server>` exposes a single server with unprefixed tool
names for drop-in use.

**Probe directly, never through a proxy**: on a host with an HTTP proxy configured, the proxy happily
answers 200/502 for *other* networks and hides the address that is really reachable (measured on ctyun:
direct to the tailnet `100.123.123.3` works, direct to the LAN does not — and the proxy is exactly the
opposite). `relay-endpoint.sh` and `install_relay_mcp.sh` therefore probe with `--noproxy '*'` and take the
first address that answers **directly** (tailnet → physical LAN → `127.0.0.1`). If the *client* carries its
own proxy settings (Hermes' httpx reads `http_proxy`), add the target IP to `NO_PROXY`; only fall back to a
stdio bridge when the client environment cannot be changed.

**Address convention**: what matters is not "Tailscale" but whether the two machines share a **private network path**.
Tailscale, EasyTier, ZeroTier, WireGuard, a subnet router or one physical LAN are the same thing to TmuxWeb: can you
reach port 8787 both ways? So the configured address is whatever that network uses (`http://100.x.y.z:8787`,
`http://10.x.y.z:8787`, ...).

`scripts/relay-endpoint.sh` does the discovery: explicit `TMUXWEB_URL` → overlay interfaces (tailscale / easytier /
zerotier / wireguard) → the physical LAN's default-route address → `127.0.0.1`, **probing each one** and returning the
first that answers. `install_relay_mcp.sh` uses it, so a host running EasyTier instead of Tailscale needs no config
either.

Hosts that must go **through a jump host** are the exception (`oracle` via `aliyun`: neither sees the other's subnet).
Set `jump_host_id` in `remote_hosts`; the relay still points at the same URL. If the host cannot reach back on *any*
network of its own, fall back to `--hub` (stdio over ssh) or `scripts/reverse_tunnel.sh`.

**Installing on a different machine** (e.g. Hermes on a Mac mini): the machine only needs to reach the URL — no repo, no
python3. Seed the token, then register, then test:

```bash
printf 'MCP_TMUXWEB_API_KEY=%s\n' '<token>' >> ~/.hermes/.env && chmod 600 ~/.hermes/.env
hermes mcp add tmuxweb --url http://100.x.y.z:8787/mcp --auth header   # answer y to both prompts
hermes mcp test tmuxweb        # -> Connected, 13 tools
```

`curl http://<address>:8787/` returning 303 means the path is open (303 is the login redirect; `/mcp` without a token
should return 401). Use the IP rather than a MagicDNS short name — `http://robo:8787/` answered 502 in testing.

### When the machine cannot be reached at all: reverse tunnel or stdio over SSH

A tailnet is the preferred answer. If the machine is neither on the tailnet nor reachable from outside
(outbound NAT, it may only dial out), there are two ways, and neither needs a change to the remote sshd:

**1) Reverse port forward** (this host dials the remote and hangs its 8787 on the remote's `127.0.0.1:8787`):

```bash
./scripts/reverse_tunnel.sh marscrazy@remote            # -R 8787:127.0.0.1:8787 by default
./scripts/reverse_tunnel.sh marscrazy@remote --ssh-port 2222 --identity ~/.ssh/id_ed25519
./scripts/reverse_tunnel.sh marscrazy@remote --print-systemd   # long-running unit for this host
./scripts/reverse_tunnel.sh marscrazy@remote --print-launchd   # macOS plist for this host
```

- Uses `autossh` when installed, otherwise a supervised loop with exponential backoff (capped at 30 s).
  `ExitOnForwardFailure=yes` fails fast when the forward cannot be set up; `ServerAliveInterval=20` /
  `CountMax=3` catch a dead link.
- The remote side binds `127.0.0.1` by default, so the remote sshd needs **no `GatewayPorts`** and the port
  is not exposed on the remote LAN. `0.0.0.0` is opt-in.
- On the remote, the MCP client then uses `http://127.0.0.1:8787/mcp`. The token is still required — the
  tunnel only fixes reachability, it does not unlock auth.

**2) stdio over SSH** (no listening port at all): MCP's stdio transport is just stdin/stdout, so the client can
use `ssh` as the transport and the relay runs **on this host**:

```bash
# this host: a wrapper reads a 0600 env file, so the token never appears in argv (visible via ps)
install -d -m 700 ~/.config/tmuxweb
printf 'TMUXWEB_URL=http://127.0.0.1:8787\nTMUXWEB_TOKEN=%s\n' '<token>' > ~/.config/tmuxweb/relay.env
chmod 600 ~/.config/tmuxweb/relay.env
cat > ~/.local/bin/tmuxweb-relay-stdio <<'SH'
#!/bin/sh
set -a; . "$HOME/.config/tmuxweb/relay.env"; set +a
exec python3 "$HOME/workspace/TmuxWeb/mcp_relay.py"
SH
chmod +x ~/.local/bin/tmuxweb-relay-stdio

# the unreachable machine: the client spawns ssh; the relay runs here
hermes mcp add tmuxweb --command /usr/bin/ssh --args <user>@<address> /home/<user>/.local/bin/tmuxweb-relay-stdio
```

`install_relay_mcp.sh --hub <user>@<address>` automates this whole recipe: it probes the URL, and if the endpoint is
unreachable it writes `~/.config/tmuxweb/relay.env` (0600, token via stdin) plus the wrapper on the hub and
registers every CLI found here as `ssh <hub> <wrapper>` — no listening port at all.
`./scripts/reverse_tunnel.sh <target> --jump <user>@<hop>` (repeatable) covers hosts that are only reachable
through a jump host; the systemd/launchd templates carry the same `-J` chain.

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
