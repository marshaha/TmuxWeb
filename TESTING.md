# 测试与验收

本文档覆盖无需真实 AI CLI 的自动测试，以及本地 tmux、远程 SSH、移动端和公网反向代理的手工验收。

### 图标与安装

- `curl -b <cookie> /assets/app-icon-32.png`、`/assets/app-icon-192.png`、`/assets/app-icon-maskable-512.png` 均 200；`/assets/app-icon-64.png` 应为 404。
- `/manifest.webmanifest` 里 `any` 与 `maskable` 两组图标都在；浏览器 DevTools → Application → Manifest 能画出图标预览。
- 登录页、主机页、分享页的 `<head>` 里也有 favicon（不要只在控制台首页放）。
- 改图标只改 `web/app-icon*.svg`，然后 `.venv/bin/python scripts/build_icons.py`（需要 `pip install cairosvg`，只在构建时用）。
- 文件面板：`Ctrl/⌘ + 点` 行应切换该项选中态且不进入目录、不触发下载；`Shift + 点` 应以最后一次点击为锚点选中整段范围；纯点击仍照旧（目录进目录、文件下载）。勾选框始终可见，点它等于选中/取消；全部选中时「全选」应变成「取消全选」。
- 文件面板：只保留列表里的 `..` 作为上级目录（工具栏不再有重复的向上按钮）；工具栏下载按钮在无选中时应打包整个当前目录，有选中时只打包选中项，按钮提示文案随选中数变化。
- 文件面板：路径栏输入 `/etc` 回车应切换目录，输入 `..` 或 `src` 相对路径也应生效，Esc 还原；
  点「多选」后勾选 2 个文件 + 1 个目录，点「下载 zip (N)」，下载的压缩包内路径应相对当前目录、目录内容完整；
  远端 pane 上同样操作应成功（远端只需 `tar`），选区超过 `TMUX_WEB_ZIP_MAX` 时应返回明确的 503 说明而不是长时间无响应。

- 远程主机表单提交：填好密码后点「测试联通」，「网络」面板里 `POST /api/remote-hosts/test` 的请求体必须带 `"credential":"<输入的密码>"`、`auth_type` 与所选方式一致；只检查输入框显隐是不够的——曾经因为 JS 取成 `fields.credential.value`（undefined）导致所有保存/测试都 422。
- 远程主机表单：密码框默认掩码，点眼睛才显示（再点还原）；表单不再有指纹确认勾选框——点「测试联通」或「添加主机」时自动扫描并记录主机指纹，指纹以灰色小字显示在表单下方，之后同一主机指纹变化时连接会被拒绝。

- 本机密钥模式：在远端把本机 `~/.ssh/id_*.pub` 加入 `authorized_keys` 后，`/hosts` 选「本机密钥（SSH Agent）」、不填凭据直接「测试联通」应成功并列出 pane；添加后 `/api/remote-hosts` 里该主机 `auth_type=agent` 且不含 `credential`。未授权时应快速失败（`Permission denied (publickey,password)`），而不是卡在密码提示。

- 新建会话：点主机行右侧「+」，弹窗预填该主机第一个 pane 的目录；填会话名后「创建」应在该主机上出现新 session 并被选中；
  重名应返回 409「session already exists」，名字非法（空格、冒号、以符号开头）或目录不存在返回 422；远程主机同样可建（pane id 带主机前缀）。
- 跨主机中继 MCP：`TMUXWEB_URL=... TMUXWEB_TOKEN=... python3 mcp_relay.py --selftest` 应列出所有 agent pane（含远端 `host~%pane`）；
  用 `initialize` + `tools/call list_agents` 走一遍 stdio，再对测试 pane 调 `ask_agent` 应拿到回答；
  对 busy pane 调 `ask_agent` 应返回 `isError` 且带 `pane_tail`；`read_agent transcript=true` 应返回 messages 数组。
- HTTP MCP 端点：`POST /mcp` 带 `Authorization: Bearer <token>` 发 `initialize` 应返回 `protocolVersion`，`tools/list` 返回 4 个工具，通知（无 id）返回 202，`GET /mcp` 返回 405，无 token 返回 401；
  `tools/call read_agent` 返回 pane 内容，参数缺失时返回 `isError` 而不是 HTTP 500。
- 免鉴权模式：`.env` 里 `TMUX_WEB_MCP_AUTH=off` 并重启后，不带 `Authorization` 的 `POST /mcp` 应返回 200，而 `/api/hosts` 仍返回 401；启动日志里应有一条未鉴权警告。改回 `on`（或删掉该变量）后 `/mcp` 恢复 401。
- 反代/CF：设 `TMUX_WEB_TRUST_PROXY=1` 后带 `CF-Connecting-IP` / `X-Forwarded-Proto: https` 请求登录页，`Set-Cookie` 应带 `Secure`；`TMUX_WEB_ALLOWED_HOSTS=chat.example.com` 时用别的 `Host` 访问应返回 421，而 `chat.example.com` 与 `127.0.0.1` 正常。

- 聊天块操作：发一条含 python 代码 / mermaid / 表格的消息（或在 `?debug=1` 下用 `window.tmuxwebUi.renderTranscript` 注入），应出现 3 个 `.block-wrap`；代码块「复制」后按钮变「已复制」，mermaid 渲染成 SVG（3 个节点），表格「源码」与「渲染」可来回切并显示 markdown 源码；消息底部「复制全部」位于卡片内、有上分隔线。代码里的 `<` `>` `&` 必须原样显示（曾经因为预转义 + marked 二次转义变成 `&gt;`），消息里的 `<script>` 必须作为文本显示（`window.__pwned` 未被置位）。

- 只读分享：创建分享时取消勾选「允许访客继续对话」，打开 `/share/<slug>` 应显示橙色「只读」徽标、底部是「只读分享：可以查看内容，但不能发送消息。」且没有输入框；`POST /share/<slug>/api/input` 返回 403 `this share is read-only`，而 `/api/transcript` 仍是 200；勾选创建的分享输入框正常且能发送。旧 `shares.db` 重启后应自动补上 `allow_input` 列（默认 1，老分享保持可对话）。

- Flutter 会话切换缓存：`cd client && flutter test` 中「a cached conversation paints before the network answers」验证有缓存时首帧就渲染旧内容并显示「更新中…」，网络返回后替换且标记消失；「the draft is kept per conversation」验证草稿按会话保留；「the cache keeps a bounded working set」验证最多 8 个（测试用 2 个）。真机：在主机树里打开 A、打开 B、再回到 A，A 的消息应立刻出现（顶部短暂显示「更新中…」），滚动位置与未发送草稿保持不变。
- 移动客户端（原生 Flutter）：`cd client && flutter test` 全绿（切块顺序与转义、表格解析/TSV、URL 规范化、cookie 登录、pane 引用编码、只读分享 403、聊天渲染与复制、块源码切换、只读隐藏输入框、发送成功/失败、登录校验与记住地址、Token 登录）。`flutter analyze` 无 error。真机冒烟：填 `http://<内网IP>:8899` + 密码 → 主机树 → 打开 `%7` → 消息可复制、表格可切源码、分享链接打开只读分享时没有输入框。构建验收：`flutter build apk --release --split-per-abi` 与 `appbundle` 成功，`apksigner verify --print-certs` 显示 `CN=TmuxWeb`。

- 远端 transcript 增量传输：`cd tests && python -m unittest test_remote_hosts -k incremental` 应通过（第二次轮询只带字节偏移、文件被重写时整窗重取、文件消失时回退定位）；`python -m unittest test_remote_hosts -k gzip` 验证 gzip 负载解码与损坏负载报错。实测一台 300MB 会话的远端主机：首次 `/api/panes/<pane>/transcript` 约 8s，之后 1-2s，响应体 ≤60KB。

- 会话切换缓存：打开两个不同的 pane（一个有 transcript、一个纯终端），来回切换——返回看过的 pane 应立即显示消息（状态先是「更新中…」再变回 `xxx transcript`），滚动位置与输入框草稿各自保留（纯终端 pane 不应串到文本 pane 的草稿）；`tests/test_tmux_web.py` 断言页面包含 `paintCachedTranscript` / `rememberTranscript` / `state.drafts`。

- 主机编辑：`/hosts` 上点「编辑」改名称/用户名后保存 → 列表立即更新，密码留空则原凭据仍可用；改主机名会重新扫描指纹；↑/↓ 调整顺序后刷新页面顺序保持，`/api/hosts` 的远端主机顺序一致；把认证方式改成密码但不填密码应报 422；保存一个连不上的配置应返回 424 且库里字段回到原值。`python -m unittest test_tmux_web -k remote_host` 覆盖这些路径。

- 远端会话换代：在远端同一个 pane 里结束一个 agent 会话并开新的（标题会变），网页应在 `TMUX_WEB_TRANSCRIPT_RELOCATE`（默认 30s）内切到新会话的消息；点刷新按钮应立即重新定位（请求带 `refresh=1`）。单测覆盖：标题变化触发重定位、超时强制重定位、`refresh=True` 丢弃增量状态（`python -m unittest test_remote_hosts -k rediscover` / `-k relocation` / `-k refresh_drops`）。

- 文件链接改写：在任意对话里出现 `[说明](/绝对路径/文件.png)` 或 `![](相对路径.png)` 时，点 `.md/.markdown` 链接会在控制台里弹出阅读面板（标题/表格/代码块照常渲染，`源码`↔`渲染` 可切换，Esc、遮罩、`关闭` 均可关闭，`新标签打开` 打开原始文件，面板内相对链接按该文件目录解析），`.json/.log` 等以等宽源码展示；其它类型点击在新标签页打开，图片直接内联显示；`https://` 链接仍是普通外链；中文/空格文件名要能正常打开（marked 会百分号编码，需先解码），`xxx.md:3685` 只打开文件本身，文件不存在返回 404。单测：`test_raw_endpoint_serves_text_inline_for_preview`、`test_raw_endpoint_requires_auth`、`test_rendered_transcript_links_are_rewritten_to_the_raw_endpoint`、`test_markdown_links_open_an_in_console_preview`。

- 发送占位：发一条消息后立刻在对话末尾出现带「发送中/已发送 ✓」的占位气泡；等 agent 回显后占位消失、只剩一条真实消息。若 agent 先回复再回显（或回显被改写），占位气泡必须留在你发送时的位置、不能漂到最新回复之后。单测：`node --test tests/js/reconcile.test.mjs`（8+ 个用例，覆盖空白差异、改写配对、按序配对、失败保留、跨 pane 隔离、锚点位置），Python 侧由 `test_optimistic_message_reconciliation_is_covered_by_node` 调用。

- 长会话回翻：选一个有几百条消息的会话，首屏 300 条，顶部出现「加载更早的消息」；滚到顶部会自动加一页（300→600→1200），加载后阅读位置不跳、加载完显示最早的消息后按钮消失。API 侧 `has_more` 在 `limit=300` 时为 true、取全后为 false。单测：`tests/test_transcript_reader.py::PagingTests`（claude/codex/hermes 的 has_more 与 limit 上限）。

- 处理中提示：给一个 agent pane 发消息（或等它正在跑），输入框正上方显示 `…thinking… Xs` 并逐秒增长（橙色跳动点），顶栏显示 `<provider> · thinking…`、状态点为橙色；agent 回复落盘后该行变回 `…idle…`、状态点恢复绿色。纯 shell pane 不显示该提示。API 侧 `waiting` 在最后一条是 user 时为 true。单测：`test_transcript_reports_an_unanswered_turn`、`test_progress_indicator_markup_is_present`。

- 分享弹窗尺寸：桌面宽度固定 520px 内，手机（390/320px）贴底全宽、圆角仅顶部、单列表单，出现分享链接后**宽度不变**、内容超长时在弹窗内部滚动，`取消`/`创建并复制链接` 始终可见；键盘把视口压到 480px 高时弹窗仍完整在视口内。单测：`test_share_dialog_is_a_fixed_size_sheet_on_phones`、`test_html_pages_are_sent_without_http_cache`。

- macOS 主机（Homebrew tmux）：用「密码」方式添加 macmini 应能连通；若确实没装，报错要给可执行提示（`brew install tmux`），而不是裸的 `zsh: command not found: tmux`。单测：`test_probe_finds_tmux_outside_the_noninteractive_path`、`test_probe_is_empty_when_tmux_is_missing`、`test_a_host_without_tmux_gets_an_actionable_error`、`test_remote_commands_export_the_resolved_binary`。

- 主机表单：桌面上「主机/IP」占满一整行且输入框满宽（编辑弹窗同样）；勾选「显示凭据内容」后密码变明文、私钥去遮蔽，取消勾选恢复；勾选「不校验连接直接保存」后用错误密码、甚至填一个不可达地址添加主机都应成功：列表显示离线、指纹位置显示「指纹待首次连通时记录」；之后把地址/凭据改对，第一次连通时会自动记录并固定指纹（`accept-new`），后续按固定指纹校验。单测：`test_a_host_may_be_saved_without_a_working_credential`、`test_host_form_lets_you_reveal_and_skip_verification`。

- 停止推理：让一个 agent pane 处于运行状态（提交一个耗时任务），输入框为空时右下按钮应变成红色方形停止键，标题为「停止当前推理（Esc）」；点击后状态显示「正在停止…」「已发送 Esc」，pane 收到 Esc 后该轮被打断；输入任意文字按钮变回发送箭头，清空又变回停止；在输入框按 Esc 也能停止。**Hermes 的 pane 会收到 Ctrl+Q**（状态角标显示「已发送 Ctrl+Q」），实测能中断当前回合且会话不退出；Claude/Codex/Kimi 收到 Esc。Flutter 客户端：thinking 且输入框为空时按钮为红色停止键，输入文字变回发送键。单测：`test_escape_can_be_sent_to_interrupt_a_turn`（含非法键 422、未登录 401）、`test_send_button_turns_into_a_stop_button_while_working`、`test_interrupt_uses_the_key_each_cli_expects`。

- Flutter 对齐：手机端应能（1）在侧边栏展开「主机 → session → window → pane」并看到 busy/idle 圆点；（2）进「服务器管理」新增/编辑/删除主机，勾「不校验连接直接保存」后错误密码也能存下；（3）在对话页右上角点终端图标进入 PTY，能敲命令并看到回显（断网后自动重连）；（4）输入框里 Enter 发送、Shift+Enter 换行。单测：`hosts_tree_test.dart`（树分组、状态圆点、Enter/Shift+Enter、`wsUri` 协议推导）、`hosts_admin_screen` 的对话框用例。

- Flutter 文件面板：对话页右上角文件夹图标进入；应能列目录（显示文件大小）、点目录进入、点「↑」回上级、点 markdown 在弹层里渲染、点下载把文件存到应用文档目录、右上角上传/打包 zip 可用。服务器管理弹窗里每个字段的标签都在输入框**上方**，不再和框内文字重叠。单测：`files_screen_test.dart`、`hosts_tree_test.dart` 里的标签位置断言。

- 主页完整预览：电脑（窗口宽 ≥1100px）点卡片右上角「↗」→ 右侧滑出抽屉，里面是完整对话（markdown 渲染、可复制、可继续发送、busy 时发送键变停止键），点右上角 ✕ 或按 Esc 关闭；把窗口缩到 1100px 以下再点同一按钮 → 直接跳到 `/preview?pane=…`，点左上角返回或用手势返回都回到 `/home`。悬停浮层现在可以用鼠标滚轮滚动，且鼠标移到浮层上不会消失。
- 主页悬停预览：把鼠标移到收藏卡片上 → 右侧浮出「agent 回复」面板，显示最后一条 AI 回复全文（超过 6000 字标注「已截断」）；若该窗格最新一条是用户消息（busy 时常见），浮层仍显示上一条 AI 回复。移开鼠标或滚动页面时浮层消失。顶栏不应再有「回到控制台」按钮（入口在左侧树顶部）。
- 主页卡片名：默认显示 `session · window · 主机`（例如 `codex · 0: codex · localhost`）；点卡片右上角铅笔改名并保存 → 卡片标题立即变化、刷新后仍在；点「恢复默认」→ 回到默认名。改未知 pane 的名称返回 404。
- 主页侧边栏：`/home` 左侧树应能看到主机/session/window/pane 与状态点；点 pane 右侧星星变为实心并在右侧「收藏」出现卡片；点树里的 pane 跳回控制台并选中；宽屏点左上角「‹」折叠（刷新后仍折叠，点 ☰ 展开）；窄屏（≤860px）侧边栏为抽屉，点 ☰ 展开、点遮罩关闭。
- tmux 路径记忆：打开主机管理页 → 每台主机显示 `tmux: /opt/homebrew/bin/tmux` 之类（没探测过的显示「tmux 路径待首次连通时记录」）；重启服务后仍然是这个值（存在 `remote_hosts.tmux_path`）；macmini 的 pane 切「终端」→ 出现真正的 zsh 提示符，不再是 `command not found: tmux`。单测：`tests/test_remote_hosts.py::test_the_tmux_path_is_saved_with_the_host`、`::test_a_saved_tmux_path_skips_the_probe`、`::test_the_terminal_attach_uses_the_resolved_tmux`。
- 状态扫描：打开控制台 → `/api/panes/status` 第一次 2–3s、之后 0.01s；日志出现 `pane state sweep: 69 panes in 2.x s`（不是 8–14s）；busy/idle 圆点与真实状态一致（有 agent 在跑的是 busy）。单测：`tests/test_remote_hosts.py::test_capture_many_batches_panes_into_one_call`。
- 增量渲染：打开一个长会话（150+ 条），让 agent 继续输出 → 新气泡追加时旧气泡的 DOM 节点保持不变（DevTools 里节点不闪、滚动位置不跳）；点「加载更早」仍然整段重绘且不丢内容。
- transcript 缓存 TTL：弱网主机连续刷新 → 首次十几秒，之后多次 0.3–2s；日志无异常。
- 慢网络：打开 oracle 的 codex pane → 首次 ~30s 内出消息（或超时后显示「读取远端 transcript 超时（30s），可能是到这台主机的网络较慢」+ 自动重试），之后增量轮询明显更快；把 `TMUX_WEB_TRANSCRIPT_TIMEOUT` 设成 5 再打开 → 很快给出网络超时提示而不是转圈。单测：`tests/test_remote_hosts.py::test_a_stalled_transcript_blames_the_link`、`::test_the_transcript_timeout_is_configurable`。
- macmini kimi pane：打开后应是聊天模式（气泡），不是原始屏幕；主线程 10 秒内 <0.1s；主页上该卡片有最后一条 agent 回复预览。把一个 agent 会话的消息清空/换到没有消息的 session → 聊天区出现「暂时没有可解析的对话」+「查看原始屏幕」，不再自动抓屏；点按钮才切终端。单测：`tests/test_tmux_web.py::test_kimi_picks_the_session_that_is_still_being_written`、`::test_an_agent_pane_does_not_fall_back_to_its_screen`。
- 影响范围：建一个 3 window 的 session（都 179×51）+ 另一个 session；手机打开第一个 window 的某个 pane 进终端 → 只有它变成 47×39，其他 window 和另一个 session 不变；回聊天模式 → 它回到 179×51，`tmux show-options -w -v window-size` 回到 `latest`（没有被钉成 manual）。远端同理（200×50 的两窗口 session 实测）。
- 终端改尺寸：桌面端拖动窗口大小、手机端键盘开合后，终端内容都必须完整（第一行是内容开头、底部有 tmux 状态栏），不能只留下最后几行。
  单测：`tests/test_tmux_web.py::test_a_pty_resize_also_signals_the_child`（断言 winsize 生效且子进程收到 SIGWINCH）。
- iOS 放大：手机端聚焦输入框（终端输入框、聊天输入框、主页搜索、主机管理表单）→ 页面不应放大，失焦后不应残留缩放。
- 键盘开合：终端模式聚焦输入框 → 顶栏仍在、终端只是变矮（不出现 58px 跳变）；收起键盘 → 终端行数/列数回到原值。
- 终端输入框是代理：敲 `echo ABC` → 终端即时回显、输入框立刻变空；再按 3 次 ← 再敲 `X` → 终端里插到光标处（`echo XABC`），**不会**重复发送整行；粘贴一段命令只出现一次。
- 组合键：点按键条 `Ctrl` → 输入框敲 `c` → 终端收到 `^C`（按键条 Ctrl 自动复位）；外接键盘 Ctrl+A 发送 `\x01`。
- 手机终端输入：终端模式 → 点输入框弹键盘 → 逐字输入能立刻在终端看到 echo；退格/回车生效；中文输入法上屏后才发送；点终端空白处键盘收起、布局回满。
- 键盘弹出/收起：终端区域高度跟随可视区（不是整屏重画），DevTools console 里 `state.termWs` 只在行列变化时收到一条 `{type:'resize'}`；滚动到中间不会跳回底部。
- 深链登录：未登录打开 `/?pane=%25xx` → 登录后回到同一个 pane；把 `next` 改成 `//evil.example` → 登录后回 `/`。
- 会话树：首次打开（或清掉 `tmux-tree-open`/`tmux-home-tree-open`）整棵树是折叠的；点某台服务器只展开它；点「展开全部」全开、点「折叠全部」全收，刷新后保持。
- 多窗口布局：顶栏「并排 / 网格 2 / 网格 3」切换 → 网格下窗口按列排、无分隔线、刷新后保持；网格 2 放 3 个窗口 → 第一行两个、第二行左边一个；手机 390px 在网格模式下仍然是整宽纵向堆叠。
- 多窗口排序：按住窗口标题栏拖到另一个窗口上（出现虚框）松手 → 顺序调换并写入 localStorage；拖到自己的位置不做任何事。
- 多窗口离屏退避：并排模式放 4 个宽窗口（把宽度拖大）→ 最右边那个 `iframe` 的 `body[data-cell-visible]` 是 `false`；横向滚回来后变 `true` 并立刻刷新。
- 多窗口终端：某个窗口点标题栏第二个按钮（切到终端）→ 该窗口变成黑底 xterm 并显示「已连接」；在里面敲命令（用专用测试 session）能正常回显；再点一次回到聊天。`is` 一个 shell pane（如 zsh）加进来时默认就是终端。
- 最大化：双击窗口标题栏 → 该窗口铺满整个页面、其他窗口隐藏、终端连接不中断（`state` 仍是「已连接」）；按 Esc 还原。
- 预设：排好 3 个窗口/网格 2 → 点「预设」→ 输入名字「保存当前」；把布局改乱后再点「预设」→「应用」→ 窗口、布局、每个窗口的聊天/终端都恢复。
- 30 秒不重载：多窗口开着终端，等 35 秒以上 → 终端的 iframe 不应重建（在 iframe 里打的标记还在、连接仍是「已连接」）。
- 控制台 ↔ 多窗口：控制台侧边栏底部「+」→ 跳到 `/grid?add=<pane>` 并把它追加成一个窗口（已在列表里则不重复）。
- 多窗口行列：点「网格」后顶栏出现 `2×1 / 2×2 / 3×2 / 2×3 / 3×3`（列×行），点了格子立刻按该形状重排；拖列接缝只改相邻两列的比例（其余列不动），拖行接缝同理；拖到最小会停在 170px；刷新后形状与比例都还在；手机 390px 下一屏一卡、没有接缝手柄。
- 侧栏小按钮：任何页面（主页/控制台/多窗口）侧边栏左下角「设置」的齿轮必须是 12×12，不能出现巨大图标或灰色残块。
- 窄窗口里的「复制」：把某一列拖到最窄（170px），窗口里对话消息下的「复制」胶囊里文字完整、不溢出边框。
- 消息时间：控制台里每条消息下方那一行，左边是时间（今天 `HH:MM`、更早 `MM-DD HH:MM`），右边是「复制全部」；用户（灰）和 agent（白）气泡都一致；390px 下不换行、不挤压；`/preview` 与多窗口里的对话同样显示。
- 图标：消息下的「复制全部」/代码块的「复制」/预览页的「复制」前面是 `icon.png` 里的双矩形图标（矢量）；控制台右上分享按钮仍是原来的 `#i-external` 箭头，主页/多窗口侧栏「管理分享」仍是各自的 `#i-share`；控制台页面里不应再出现 `ico-mask` 或 `/assets/ic-`。
- 旧检查（已废弃）：控制台消息下的「复制全部」、代码块的「复制」、预览页的「复制」前面都有双矩形小图标；控制台右上分享按钮、主页/多窗口侧栏的「管理分享」是「向右箭头带弯尾巴」；hover 时图标颜色跟着按钮变（说明是 currentColor 遮罩而不是固定色图片）。
- 设置面板：控制台左下角只剩「设置」（没有单独的退出按钮）；面板最上面两行「服务器管理」「分享管理」直接跳 `/hosts`、`/shares`；面板刚打开时**任何一行都不该有焦点框**（不要看到蓝色/黑色描边），用 Tab 才出现橙色描边；下面还有「修改密码」「导入 / 导出」两行折叠项与「退出登录」+「关闭」。
- 发送重发：选一个会话 → 断网（DevTools Offline）发一条 → 气泡下显示「发送失败 重发」→ 恢复网络点「重发」→ 变成「已发送 ✓」，对端只收到一次。
- 掉线卡片：收藏一个会话让 `/api/home` 缓存到预览，再让它不可达（或直接用 `TMUX_WEB_HOME_ENTRY_SECONDS=0` 的测试）→ 卡片状态点变「离线」，点开预览面板标题是「离线 · 缓存 · N 分钟前」，正文仍是最后一次内容；没有缓存时显示「这个会话暂时没有可预览的内容。」。
- 多窗口模式记忆：加一个 shell 窗口（如 zsh）→ 自动退化为 terminal；手动切回 chat 后刷新仍是 chat；把该窗口移出再加回来，选择依然保留（`explicit` 按 pane 保存，不受窗口列表影响）。
- 手机多窗口（375/390px 宽）：顶栏不出现横向滚动（`document.documentElement.scrollWidth === clientWidth`），只有图标没有文字；卡片一屏一个、高度 `100dvh-64px`；新加的 shell 窗口默认 `chat`，卡片里是「这个 pane 没有结构化对话…」+「切到终端（可输入）」+ 可滚动屏幕；点标题栏切换终端后刷新页面仍是终端（`explicit` 记住了选择）。左侧不应出现灰色残影（抽屉阴影只在打开时出现）。
- 树↔窗口：在 `/grid` 展开某台主机 → 已在窗口里的 pane 行是浅橙底 + 左侧橙线；再点它 → 窗口数不变、该窗口闪一下；点没有标记的行 → 新增窗口并立刻带上标记。
- 多窗口外壳：打开 `/grid` → 左侧是与主页相同的会话树侧边栏（标题/图标行/展开折叠/树/底部），宽度与主页共享（`tmux-side-w`）；点树里任意 pane → 该 pane 立刻加成一个窗口；点侧边栏的「折叠」→ 侧栏消失、工具条左侧出现 ☰ 可再打开；手机 390px → ☰ 打开抽屉、点遮罩关闭。
- 多窗口（`/grid`，桌面 ≥ 1200px）：点「添加窗口」搜索并加入 2–3 个会话 → 各占一列、状态点有颜色、对话能滚动、底部输入框可用；拖动两列之间的分隔线 → 宽度变化并在刷新后保持；点某窗口的 ✕ → 该列移除、其余列均分宽度；点「在控制台打开」→ 跳到 `/?pane=…`。手机 390px → 窗口变整宽纵向堆叠、无横向溢出、分隔线隐藏。未登录访问 `/grid`（以及 `/home`、`/preview`）→ 303 到 `/login?next=…`，登录后回到原页。
- 卡片预览：点收藏/最近访问/看板任意卡片 → 出现预览框（桌面在卡片右侧，手机底部抽屉），框里「完整预览 / 聊天 / 关闭」三个按钮分别跳 `/preview`、控制台、关闭；点框外或 Esc 也关闭。卡片上不再有单独的 ⤢ 按钮。
- 手机卡片（旧行为，已由上面的预览框取代）：在 390px 视口点收藏/最近访问卡片 → 直接进控制台并打开该会话（不应弹出预览浮层）；点卡片上的 ⤢ → 进 `/preview` 页；预览页顶栏有聊天和终端两个入口。
- 视图切换：顶栏星星左边有聊天/终端两个图标按钮，点击互切且高亮跟随；侧边栏里不再有这组按钮；390px 宽度下顶栏仍是一行、不溢出。
- 预览页：`/preview?pane=…` 顶栏有聊天和终端两个入口（都在星星左边），终端入口打开的地址带 `mode=terminal`，落地即终端视图。
- 首页进入：清掉 `localStorage['tmux-last-pane']` 后打开 `/` → 自动跳到 `/home`；在有「上次 pane」的情况下打开 `/` → 留在控制台并恢复那个会话；任意打开一个 pane 后回主页 → 「最近访问」里出现它（最多 4 个，最新在前）。
- busy 判定：`GET /api/panes/status` 里每个 pane 带 `status_source`；agent pane 多数是 `session`（会话文件结论），shell pane 是 `screen`/`activity`。
  让某个 claude/codex/kimi pane 真正跑一个回合 → 它应变 busy（`session`）；跑完 → idle。文件不再更新却停在 busy 的（中断的会话）应显示 idle。
  Kimi 状态栏写着 `thinking` 时不应误判 busy。单测：`tests/test_agent_status.py`（含 Python 与远端 shell 规则一致性）。
- busy 判定：Kimi pane 空闲时状态应是 idle（状态栏里有 `thinking: high` 也不会误判）；agent 正在跑时出现 `esc to interrupt` 才是 busy；纯日志 pane 是灰点 unknown。单测：`tests/test_relay.py::test_a_kimi_status_bar_is_not_a_running_turn`、`::test_plain_output_is_unknown_not_busy`、`::test_soft_progress_needs_an_ellipsis`。
- 主页秒开：先正常打开一次 `/home`（写入缓存），再在控制台 ↔ 主页之间来回切 → 主页内容立刻出现、不会空白；用 DevTools 把 `/api/home` 设成离线/慢速 → 仍然显示缓存卡片并带「显示缓存，正在更新…」，恢复后自动去掉；清掉 `localStorage['tmux-home-cache-v1']` 再拦接口 → 显示骨架屏（统计胶囊 + 卡片占位）。后端：给一个不可达主机上的 pane 加收藏 → `/api/home` 1–2 秒返回且该卡片 `pending=true`，其余卡片正常。单测：`tests/test_home_board.py::test_the_board_answers_before_a_slow_host_does`、`::test_the_board_is_painted_from_a_local_snapshot`。
- 侧边栏主机刷新：每台主机行右侧有刷新按钮 → 只刷新这一台（网络上几百毫秒），树不会折叠、选中的 pane 不变、其他主机数目不变；主机离线时行内提示错误。单测：`tests/test_tmux_web.py::test_refreshing_one_host_leaves_the_others_alone`、`tests/test_home_board.py::test_every_host_row_offers_a_refresh`。
- thinking 提示位置：在控制台打开一个 busy 的 agent pane → 「thinking + 秒数」显示在对话区底部居中、输入框上方，不遮挡消息；顶部状态区显示「kimi · thinking…」。
- 传输窗口自适应：弱网主机（oracle）连续刷新 transcript → 首次十几秒，之后明显变快；日志里出现 `transcript window for oracle: 4096 KB -> 2048 KB`。单测：`tests/test_remote_hosts.py::test_the_transcript_window_adapts_to_the_link`。
- 对话右上角星标：选中一个未收藏的 pane → 右上角五角星是空心的；点一下 → 变实心、提示「取消收藏」，主页「收藏」里出现这张卡；再点一下 → 回到空心并从主页消失；在左侧树里点星标 → 右上角按钮同步变化；切到已收藏的 pane → 按钮直接是实心；没选 pane 时按钮禁用。打开 `/preview?pane=%257` → 头部星标能同样收藏/取消。单测：`tests/test_home_board.py::test_an_open_conversation_has_a_star_in_its_top_bar`。
- 侧边栏宽度：把侧边栏右边缘往右拖 → 变宽且松手后刷新仍保持；往左拖到极限 → 停在 200px；双击边缘 → 回到默认（控制台 280 / 主页 268）；拖到最窄（200px）→ 顶部图标按钮换成两行、全部可点，没有被裁掉一半的图标；折叠侧边栏再展开 → 宽度仍是刚才拖的；手机宽度（≤860px 主页 / ≤760px 控制台）→ 抽屉宽度固定、没有拖动手柄。单测：`tests/test_home_board.py::test_the_sidebar_width_is_draggable_and_remembered`。
- 主页：在控制台点某个 pane 右侧的五角星 → 图标变实心；打开右上角房子图标（或 `/home`）→ 该 pane 出现在「收藏」，卡片上有状态徽标、最近一条对话预览；点卡片回到控制台且该 pane 已选中；在主页点卡片右上角星星 → 卡片消失并显示空状态。在主页搜索框输入对话里出现过的词 → 结果按「服务器 / tmux session / pane / 对话内容」分组、命中片段高亮，点 pane 或对话命中跳回对应 pane；输入 `^codex` 这类正则 → 结果页标「匹配方式：正则」且只命中以 codex 开头的 session；输入 `Review(` → 标「正则无效，已按字面匹配」且不报错；输入已添加的服务器名 → 出现在「服务器」组；顶栏没有「远程主机 / 管理分享 / 刷新」，这三项和控制台一样在侧边栏顶部一排图标按钮里。用 MCP 调 `publish_home_card` → 主页「看板卡片」出现新卡；调 `remove_home_card` → 卡片消失。单测：`tests/test_home_store.py`、`tests/test_home_board.py`。

- 刷屏 pane 性能：新建一个日志 pane（`tmux new -d -s perf 'while true; do echo line; done'`）→ 在控制台选中它，聊天模式应保持 50fps 以上、主线程耗时明显低于 1s/10s，屏幕只显示最近几百行并出现「只显示最近 600 行」提示；切到终端模式同样不卡，积压过多时会插入「输出过快，已跳过 N 字节」。顶栏 ⏸ 暂停后屏幕停止刷新（无 CPU 占用），▶ 继续后恢复。侧边栏每 20 秒才刷新一次，滚动位置不会再被重置。`/api/hosts` 第二次调用应命中快照（毫秒级），点刷新按钮才重新探测远端。单测：`test_hosts_list_is_snapshotted_between_polls`。

- 免保存跳板机：添加主机时「跳板机」下拉保持「直连」，在下面的输入框填 `ops@bastion.example:2222` → 保存成功，列表显示「经由 ops@bastion.example:2222」；填 `host:abc` 这类非法值返回 422；同时选下拉又填输入框也返回 422。生成的 ssh 命令行里应出现 `ProxyCommand` 指向该地址，凭据用的是目标机自己的那份。
- 多级跳板：建三台可连的主机 A、B、C，让 B 的跳板机选 A、C 的跳板机选 B → `/api/remote-hosts` 里 C 的 `jump_path` 应为 `B -> A`，列表显示「经由 B -> A」；选中 C 下的 pane 仍能列目录/读内容（实际会依次拨 A、B、C）。删除 B 后 C 变成「经由 A」，再删除 A 则直连。把 A 的跳板机设成 C 会返回 422（成环）。
- 跳板机：`/hosts` 页新增「跳板机」下拉 → 选另一台已保存主机再添加目标机，保存时走 `ProxyCommand`，列表里该行显示「经由 xxx」。目标只能经跳板机访问时也应能列出 pane 并读取内容；把跳板机删除后该主机自动回退为直连。保存一个指向自己/未知 id 的跳板机会返回 422。MCP：`add_remote_host`（可带 `jump_host`）+ `list_remote_hosts` 应能由 agent 独立完成添加与确认。单测：`test_jump_hosts_are_validated_and_cleared_on_delete`、`test_a_jump_host_becomes_a_proxy_command`。

## 1. 安装测试依赖

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt httpx
```

`httpx` 仅用于 FastAPI `TestClient` 测试。

## 2. 自动化测试

```bash
python -m py_compile *.py tests/*.py
python -m unittest discover -s tests -v
```

自动测试使用 fake backend 和临时数据库，不会连接真实 SSH 主机，也不会向真实 tmux/AI 会话发送消息。覆盖范围包括：

- FastAPI 登录和 API 权限（Cookie 与 `TMUX_WEB_API_TOKEN` Bearer/Header 认证）
- tmux pane 枚举、capture、输入和 WebSocket
- PTY resize/关闭
- Claude/Codex/Hermes/Kimi transcript 解析与 JSONL 尾部读取
- 分享创建、密码、过期、续期、停止和删除（本地与远端 pane）
- 远程主机加密存储、host scoped pane ID 和 SSH 参数构造
- SSH 连接复用参数、mux 目录权限、可用环境变量关闭
- 远端摘要轮询（内容未变不传输正文）、远端 transcript JSON 解析与错误传播
- 中继：状态判定、标记协议、忙/超时（504 带尾部）、跨主机转发、中继日志
- 文件面板：目录排序/隐藏文件/截断、路径与非法文件名拒绝、下载头、上传字节一致性、上传大小限制、远端 helper 与 SSH 传输参数
- provider/能力位（`capabilities`）暴露
- 密码不进入 argv/普通环境变量
- 公钥误当私钥时的拒绝
- 远程 tmux 字段分隔符回归测试（不使用 Tab/控制字符）
- MCP mailbox 协议与身份限制

GitHub Actions 会在 Python 3.11、3.12 和 3.13 上执行同样的编译和 unittest。

## 3. 本地 tmux 冒烟测试

创建一次性测试 pane：

```bash
tmux new-session -d -s tmuxweb-smoke 'python3 -q'
tmux list-panes -t tmuxweb-smoke -F '#{pane_id} #{session_name}:#{window_index}.#{pane_index} #{pane_current_command}'
```

启动 TmuxWeb 后验证：

1. 登录成功；错误密码返回 401。
2. 左侧出现 `localhost → tmuxweb-smoke → window → pane`。
3. 聊天/capture 模式能看到 Python 提示符。
4. 关闭“发送后按 Enter”，输入 `print("PASTE_ONLY")`，确认只粘贴、不执行。
5. 开启提交，发送 `print("SUBMIT_OK")`，确认出现 `SUBMIT_OK`。
6. 切换终端模式，输入和窗口 resize 正常。
7. 关闭页面后执行 `tmux list-sessions`，原 session 仍存在。

清理：

```bash
tmux kill-session -t tmuxweb-smoke
```

不要在用户正在工作的 AI pane 上做输入测试。

## 4. 结构化 transcript 验收

分别选择正在运行的 Claude Code、Codex、Hermes 和 Kimi Code pane：

- Claude：确认 user/assistant 消息顺序、Markdown 表格和代码块正确；compact summary 不显示为普通用户消息。
- Codex：确认 pane title/thread 能定位对应 rollout。
- Hermes：确认按 pane cwd 选择最近活跃 CLI session。
- Kimi Code：确认按 pane cwd + title 绑定 `state.json`/`wire.jsonl`，并过滤 thinking、tool output 和内部 notification。
- 未识别 provider：确认自动回退到 ANSI capture，而不是猜测 transcript。

对大 JSONL 文件进行并发测试时，监控服务 RSS，确保内存随响应上限而不是 transcript 文件大小增长。

## 5. 远程 SSH/tmux 验收

使用专门的测试账户，不要使用生产 root 凭据。

1. 打开 `/hosts`。
2. 扫描 host key；通过远端控制台或已知记录核对 SHA256 指纹。
3. 勾选确认后运行“测试联通”。
4. 确认失败测试不会保存主机。
5. 正确凭据应返回 pane/session 数量。
6. 保存后主机树中应出现远端 pane。
7. 验证 capture 和文字发送。
8. 修改或替换 host key 后必须拒绝连接。
9. 删除主机后列表 API 不再返回该记录。

远端交互 shell 可见 tmux、页面却显示 0 pane 时，使用相同 SSH 用户和非交互环境检查：

```bash
ssh user@example-host 'id -un; command -v tmux; tmux list-sessions; tmux list-panes -a -F "#{pane_id}|#{session_name}|#{window_index}|#{pane_index}|#{pane_current_command}"; printf "PATH=%s\nTMUX_TMPDIR=%s\n" "$PATH" "$TMUX_TMPDIR"'
```

确认 tmux socket 属于登录用户；不同用户默认不能看到同一个 tmux server。

## 6. 远端渲染与性能验收

远端 pane 的聊天视图和结构化 transcript 必须和本地一致：

1. 远端 pane 标题含中文时，`/api/hosts` 返回的 `title` 必须是原文，不是 `____`。
2. 选择远端 Claude/Codex/Kimi/Hermes pane，聊天视图应显示结构化 transcript（状态栏显示 `<provider> transcript`），而不是 ANSI capture。
3. 远端 transcript 不依赖 python3；在只有 sh/grep/tail 的最小环境里仍应能出结构化会话。Hermes 若既无 session jsonl 也无 sqlite3，应回退 capture 且页面不报错。
4. 指向远端 pane 的 `GET /api/panes/<host_id>~%N/capture` 与本地同样返回内容。
5. 观察 SSH 进程：空闲 pane 的实时视图应只有每 0.4–2 秒一次的短命令（`cksum` 摘要），不应持续传输整屏文本：

```bash
# 在另一终端观察
ps -ef | grep 'ssh .*tmux capture-pane' | grep -v grep
```

6. 同时打开 3 个远端 pane 的实时视图，`ssh` 连接数应保持 1 个（ControlMaster 复用）：

```bash
ls -l "${XDG_RUNTIME_DIR:-/tmp}/tmuxweb-mux-$(id -u)/"
```

7. 关闭复用（`TMUX_WEB_SSH_MUX=0`）重启后功能仍应正常，只是延迟上升。

## 7. 跨服务器中继验收

准备一个不会调用模型的假 agent pane（已在仓库中）：

```bash
tmux new-session -d -s relay-test "python3 -u tests/fake_agent_pane.py"
tmux list-panes -a -F '#{session_name} #{pane_id}'
```

然后：

```bash
export TMUXWEB_URL=http://127.0.0.1:8787
export TMUXWEB_TOKEN=...            # .env 里的 TMUX_WEB_API_TOKEN
./relay_client.py peers             # 应看到 relay-test pane，状态 idle
./relay_client.py ask %N '1+1 等于几？' --timeout 30
# => fake answer to: 1+1 等于几？

./relay_client.py ask '<host_id>~%N' '跨服务器提问' --timeout 30   # 同一 pane 走 SSH 通道
```

远端首次读取 transcript 时，会把 `transcript_reader.py` 缓存到远端的
`${XDG_RUNTIME_DIR:-/tmp}/tmuxweb-reader-<hash>.py`（0600，按内容 hash 命名，升级后自动换新）。

再验证边界：

1. 目标 pane 正在生成时（有 spinner / `esc to interrupt`）`ask` 返回 409；加 `--force` 才会发送。
2. 目标 agent 不遵守标记协议时，`ask` 返回 504，并在 `tail` 里给出 pane 尾部内容。
3. 同一个 pane 并发两个 `ask`，第二个返回 409。
4. `/api/relay/log` 能看到 OK/超时记录（最多 100 条，重启即清空）。
5. 真实 CLI 冒烟：选一个空闲的 Claude/Codex/Hermes pane，`ask` 一个极短问题（如“只回答四个字：中继已通”），确认能拿到结构化回答；不要在用户正在使用或有未保存输入的 pane 上测试。
6. 用错误 token 调用应返回 401；不设置 `TMUX_WEB_API_TOKEN` 时 token 认证关闭，只能使用 Cookie。

清理：

```bash
tmux kill-session -t relay-test
```

## 8. 文件面板验收

先把某个 pane 切到一个测试目录（不要用生产目录）：

```bash
mkdir -p /tmp/tmuxweb-files/sub && echo hello > /tmp/tmuxweb-files/a.txt
tmux new-session -d -s filetest -c /tmp/tmuxweb-files 'bash'
```

1. 选中该 pane，点侧边栏「▤」：目录在前、文件在后，`a.txt` 有大小，隐藏文件默认不显示。
2. 点 `sub` 进入、点 `..` 返回，`↑` 按钮同样可用。
3. 点 `a.txt` 应触发下载，文件名与内容一致；中文文件名不乱码。
4. 把文件拖到面板上，或点「⇧」多选上传：文件出现在当前目录，字节一致（二进制文件也一致）。
5. 上传超过 `TMUX_WEB_FILE_UPLOAD_MAX` 的文件返回 413；名字含 `/`、`..` 返回 422。
6. 远端 pane 重复 1–4，确认走 SSH 也能列目录、下载、上传。
7. 在没有 python3 的远端（或 `env -i sh -c` 之类的最小 shell）上验证列目录仍然可用：文件管理只依赖 `find`/`stat`/`head`/`cat`。
8. 断网/远端离线时面板显示错误信息，不白屏；恢复后点「↻」可刷新。
9. 远端 transcript 会随远端文件更新：在远端会话里产生一条新消息，页面应在 1–2 秒内自动出现，不需要刷新；同时远端空闲时每秒只有一次 `stat`（可在服务端日志/`ps` 观察 `ssh ... stat`）。

清理：`tmux kill-session -t filetest; rm -rf /tmp/tmuxweb-files`

## 9. 分享生命周期验收


使用一次性本地 Claude 测试会话：

1. 任何 pane 都能创建分享：codex/kimi/hermes 按消息渲染；纯 shell pane 打开分享页应显示等宽屏幕文本块（`/share/<slug>/api/transcript` 返回 `mode=screen`），并且输入框能继续往该终端打字。
2. 创建带密码、短有效期的分享。
2. 在无管理员 Cookie 的浏览器/隐私窗口打开链接。
3. 错误密码应返回 401，正确密码后可读 transcript。
4. 访客不能访问 `/api/hosts`、`/hosts`、`/shares`。
5. 重置密码后，旧访客 Cookie 立即失效。
6. 续期后有效期增加。
7. 停止后访客 API 返回 410。
8. 删除后返回 404。

不要把验收消息发送到真实用户会话。

## 10. 移动端/PWA 验收

在 iOS Safari 和 Android Chrome 至少检查：

- 页面打开时不自动弹出键盘。
- 输入框获得焦点后保持可见。
- 移动端 Enter 换行，点击发送按钮才提交。
- IME 组合输入不会误发送。
- 侧栏可折叠，长代码和 Markdown 表格只在气泡内部横向滚动。
- 终端快捷键和方向控制可用。
- 关闭软键盘后终端重新 fit，没有黑色残留区域。
- 安装为 PWA 后可打开；离线缓存中不包含 transcript、API 或登录页面。

## 11. 公网反向代理验收

在 HTTPS 入口验证：

```bash
curl -I https://your-host.example/login
```

检查：

- `/login` 可访问，受保护页面未登录时重定向或返回 401。
- API、WebSocket 和静态管理资源均受认证保护。
- SSH/tmux 依赖错误保留 JSON detail，不被代理替换成通用 502。
- Service Worker 不缓存敏感请求。
- 本地源站停止时，公网监控能明确报告 origin 不可用。

## 12. 发布前隐私检查

```bash
git status --ignored --short
git ls-files

git grep -nE 'BEGIN (OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY|password\s*=|api[_-]?key\s*=|token\s*=' -- . ':!tests/**' || true
```

确认以下内容未被 Git 跟踪：

- `.env`
- 数据库及 WAL/SHM
- SSH 私钥和 `.remote_hosts.key`
- Cookie、日志和 capture
- 真实公网 IP、域名、用户名
- Claude/Codex/Hermes transcript

- iOS 键盘：在 iPhone Safari/主屏打开终端 → 点终端弹出**自绘键盘**（qwerty / shift / 123 / del 长按连删 / 空格 / ret），**系统键盘不弹、白条不出现**；用自绘键盘敲 `echo ok` + ret → pane 收到并执行；点 `系统键盘` → 系统键盘出现（白条也出现，那是 iOS 自身的），之后默认用系统键盘（`localStorage['tmux-kb-custom']='0'`）；`?kb=custom` 可强制回自绘；`?kb=auto` 应恢复「点终端即起键盘」。非 iOS（桌面/安卓）行为不变，点终端直接起键盘。
- iOS 键盘助手条：iPhone 上用 Safari/添加到主屏打开终端并点终端起键盘 → 键盘上方**不应**出现「↑ ↓ ✓」那条白条（那是 WebKit 给表单控件的表单助手条）；若仍出现，访问 `/?pane=…&mode=terminal&kb=input` 对比，确认是 contenteditable 还是 input 触发的。
- PWA 标签：`curl -s <host>/manifest.webmanifest | jq '{display,shortcuts}'` 应有 `standalone` 与两个快捷方式；`/`、`/home`、`/grid`、`/preview`、`/term`、`/hosts`、`/shares` 都带 `manifest.webmanifest`、`apple-touch-icon`、`apple-mobile-web-app-capable`、`theme-color`。iPhone Safari「分享 → 添加到主屏幕」后从图标打开应无地址栏。
- 桌面终端：1440px 桌面浏览器进入终端 → 底部**没有**任何辅助键条，终端铺满（实测 1140×822、`display:none`）；窗口缩到 760px 以下才出现键条。
- 手机终端（390×780）：进入终端 → 底部只有一行按键（shift tab / 粘贴 / ? / / / esc / tab / ctrl / alt / 方向键 / ^C，可横向滑动），右侧常驻「更多」和「键盘」图标；**页面上没有输入框**。点终端任意位置 → 隐藏输入框获得焦点（真机上软键盘弹出）、终端高度收缩而顶栏仍在。点 / 和字母 → pane 里出现对应字符；点 ctrl 再点 c → 发出 ^C（pane 里看到 C:130 之类的取消提示）。点「更多」→ 四列扩展键面板出现、系统键盘收起、终端仍在面板上方可见；再点一次收起并重新聚焦。
- 手机终端缩放：双指捏合 → 字号变化（9–22px）、列数重排、`localStorage['tmux-term-font']` 记录；刷新后保持。
- 深链竞态：远端主机还在扫描时访问 `/?pane=<远端 pane>&mode=terminal` → 扫描完成的那一刻自动选中该 pane 并进入终端（旧行为会退回到上次的 pane）。
