# TmuxWeb

[English](README_EN.md) · [测试与验收](TESTING.md) · [Testing](TESTING_EN.md)

TmuxWeb 是一个自托管的 tmux 与 AI CLI 会话管理中心。它把本机及 SSH 远程主机上的 tmux pane 组织为 `主机 → session → window → pane`，并提供 ChatGPT 风格聊天、真实 PTY 终端、结构化会话记录和受控分享。

> 安全提醒：TmuxWeb 能向 tmux pane 输入文字并提供交互终端，等同于访问运行服务的系统账户。不要直接暴露到公网；请使用 HTTPS、访问控制、强密码和防火墙。

## 功能

- **多主机管理**：一个本地服务管理 localhost 和 SSH 远程主机；远端只需 SSH 与 tmux，不需安装守护进程。
- **主机表单**：「主机/IP」独占一整行、输入框满宽；「显示凭据内容」勾选框可把密码转明文、私钥去掉遮蔽；「不校验连接直接保存」允许密码/密钥有误、甚至主机暂时不可达时先把主机存下来（列表显示离线、指纹显示「待首次连通时记录」），该主机第一次真正连上时自动记录并固定指纹（TOFU，`StrictHostKeyChecking=accept-new`），之后按固定指纹校验；稍后改好凭据即可正常使用。
- **macOS 的 tmux 路径**：ssh 非交互命令看不到 Homebrew 的 PATH，因此首次连接会把 tmux 解析成绝对路径并缓存（`command -v` → `/opt/homebrew/bin` → `/usr/local/bin` → `~/bin` → `bash/zsh -lc`），之后每条远端命令都带 `TMUX_BIN`；确实没装时报错会提示 `brew install tmux`；tmux 装在别处时可用 `TMUX_WEB_TMUX_PATH` 指定。
- **tmux 树导航**：按主机、session、window、pane 展示；展开状态保存在浏览器本地。
- **AI 聊天视图**：Claude Code、Codex、Hermes、Kimi Code 读取结构化 transcript（本地和 SSH 远端都能读）；无法解析时回退 ANSI `capture-pane`。
- **跨服务器中继**：任意 pane 里的 agent 可以通过 HTTP API / `relay_client.py` 向另一台服务器上的 agent pane 提问并拿到回答，TmuxWeb 负责注入、等待和回收答案。
- **远端渲染优化**：SSH 连接复用（ControlMaster）、远端内容摘要轮询、空闲退避，远端 pane 的中文标题和内容不再被本地化设置改写成下划线。
- **文件面板**：侧边栏显示当前 pane 工作目录的文件树（可进入子目录、逐级返回、滚动），支持点击下载和拖拽上传，本地与 SSH 远端一致。
- **消息操作**：每条消息右下角「复制全部」复制整条原文；消息内每个块（代码 / mermaid / 表格）有独立工具条——「复制」复制块内容（表格复制为 TSV，mermaid 复制源码），表格与 mermaid 还可「源码 / 渲染」互切。mermaid 用本地 vendored 的 mermaid.js 懒加载渲染（只有出现 mermaid 块时才下载 3MB）。
- **真实终端**：xterm.js + PTY attach，支持输入、resize、ANSI 和移动端快捷键。
- **安全发送**：文字经 tmux buffer 原样粘贴，不拼接 shell 命令；提交时处理旧草稿和 bracketed paste。
- **新建会话**：主机行右侧的「+」可在本机或任意远程主机上创建新的 tmux 会话（`tmux new-session -d`），
  弹窗里填会话名（字母/数字/`.`/`-`/`_`，1–64 字符）和可选工作目录，目录不存在会直接报错；建完自动展开并选中该 pane。
- **远程连接**：支持密码、私钥，以及直接使用本机密钥/SSH Agent（远端已授权该公钥时不必再输入凭据）；固定 SSH host key 指纹；密码/私钥使用独立 Fernet 主密钥加密保存（本机密钥模式不保存任何凭据）。
- **会话分享**：可设置 slug、密码、有效期，并可选择**只读 / 可对话**；支持续期、重置密码、停止和删除；**任何 pane 都能分享**——有结构化 transcript 的（Claude / Codex / Kimi / Hermes）按消息渲染，纯 shell 则按屏幕文本渲染且可继续输入。
- **PWA/移动端**：适配软键盘、安全区和窄屏；Service Worker 不缓存 API、transcript 或登录状态。
- **图标**：`web/app-icon.svg` 是唯一源文件（深色圆角底 + 米白 `>` ＋ 陶土色块光标），`scripts/build_icons.py` 从它渲染出 32/180/192/512 的 PNG 与 maskable 变体；所有页面（含登录页）都带 favicon，manifest 里 `any` 与 `maskable` 分开声明，安卓圆形裁剪不会切到图案。
- **文件面板**：路径栏可直接编辑（绝对路径或相对 pane 当前目录，回车跳转、Esc 取消，聚焦时全选），工具栏的下载按钮直接打包当前目录（有选中则只打包选中项）；每行都带勾选框，`Ctrl/⌘ + 点` 切换单项、`Shift + 点` 选范围（桌面习惯），选中后页脚出现「全选 / 清空 / 下载 zip (N)」，文件或整个目录都能打包（本地用 zipfile，远端只需 `tar`，拉回字节后本地转 zip）；解压总量上限 `TMUX_WEB_ZIP_MAX`（默认 128MB）、文件数 `TMUX_WEB_ZIP_MAX_FILES`（默认 1000）。
- **受控 MCP 邮箱**：可选的 Claude Code、Codex、Hermes 文本消息中继，不开放任意 shell/文件能力。

## 架构与边界

- 聊天模式不创建长期 `tmux attach`，避免多个客户端参与 tmux 尺寸协商。
- 只有终端模式使用 PTY attach；WebSocket 断开后释放。
- 远端结构化 transcript 也不依赖远端 python3：远端只跑一段 POSIX sh 定位会话文件（Claude 项目目录 + 标题/屏幕探测、Codex session_index、Kimi state.json、Hermes `--resume` 的 session jsonl 或 `sqlite3`），把文件尾部字节拉回本地，用**本地同一套解析器**解析——等同 `scp` 拉回来再解析。定位不到时回退远端 `capture-pane`。远端会话由前端按 1 秒节奏轮询：服务端按 (主机, pane, limit) 缓存解析结果，2 秒内重复请求不打远端，之后只用一次 `stat` 判断文件是否变化，变了才重新拉取——所以远程对话跟着远端文件实时更新，空闲时几乎无开销。
- 远端 transcript 的传输是增量且压缩的：首次定位只扫最新 3 个会话文件的尾部 256KB（不再 grep 整个几百 MB 的文件），之后每次只按字节偏移拉取**新增部分**（`tail -c +offset`），并用 `gzip` 压缩上线——实测一台 300MB 会话、0.7s RTT 的主机：首次 53s → 8.6s，后续轮询 1.5s 左右，单次传输从 4MB 降到几十字节到几百 KB。窗口大小可用 `TMUXWEB_PAYLOAD_BYTES` 调整。
- 前端会缓存每个 pane 的 transcript、滚动位置和未发送草稿：切换回看过的会话时立刻从缓存渲染（实测 0.02–0.14s），同时在后台按 1 秒节奏重新校验，只有签名（mtime + 消息数 + 最后一条 id）变化时才重绘；最多保留 8 个 pane 的工作集。这样在多窗口之间来回切换时不会每次都白屏等待远端。
- 远端会话文件会**定期重新定位**：client 端记住了上次的会话文件并按字节增量读取，但 agent 在同一个 pane 里开新会话会写新文件；为此每次 tail 会顺带读取 pane 标题，标题变了（或在 `TMUX_WEB_TRANSCRIPT_RELOCATE` 秒后，默认 30s）就重新定位一次，避免一直读旧文件。网页版与客户端的刷新按钮会带 `refresh=1`，直接丢掉增量状态与缓存。
- 远端 pane ID 带主机作用域，避免不同 tmux server 的 `%0` 冲突。
- 远端 tmux 调用统一带 `LC_ALL=C.UTF-8`，字段分隔符使用可打印字符串，因为 tmux 在非 UTF-8 locale 下会把控制字符和非 ASCII 字段改写成 `_`。
- 实时视图先取远端 `cksum` 摘要，内容未变就不传输正文；空闲后轮询间隔退避到 2 秒。
- 当前管理登录是单用户模型；多用户场景还需要用户级主机授权与审计。

## 文件浏览与上传下载

侧边栏顶部「▤」按钮切换到当前 pane 工作目录的文件视图；再点一次（或点面板里的「✕」）就把面板收起来，侧栏回到 pane 树。开关状态会记在浏览器里，刷新后按上次的布局恢复（连同上次选中的 pane）。

侧边栏顶部「▤」按钮切换到当前 pane 工作目录的文件视图：

- 目录在前、文件在后，点目录进入、点「..」返回上级；隐藏文件默认不显示。
- 点击文件名即下载（`Content-Disposition: attachment`，支持中文文件名）。
- 把文件拖到面板里即上传到当前目录；也可以用面板上的「⇧」按钮多选上传。
- 本地 pane 直接读文件系统；SSH 远端完全用 POSIX 工具：目录清单靠远端 `find`（GNU 有 `-printf` 就用，否则回退 GNU/BSD `stat`），下载 `head -c`，上传 `cat >`。**远端不需要 python3、不需要装任何东西**，等同 scp 的依赖水平。
- 上传先写临时文件再原子替换，避免半截文件覆盖原文件；单文件上限由环境变量控制（默认下载 64MB、上传 256MB）。

HTTP 接口（需登录 Cookie 或 `TMUX_WEB_API_TOKEN`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/panes/{pane}/files?path=&hidden=&limit=` | 列目录，`path` 省略时用 pane 的 cwd |
| GET | `/api/panes/{pane}/files/download?path=` | 下载单文件 |
| PUT | `/api/panes/{pane}/files/upload?dir=&name=` | 请求体为文件原始字节 |

> 注意：文件读写等同于服务运行账户的权限，远程主机上同样如此。只给可信的人开端口。

## 跨服务器中继

任何能访问 TmuxWeb 的进程都可以中继，典型用法是让 A 机器上的 agent 去问 B 机器上的 agent：

```bash
export TMUXWEB_URL=https://tmux.example.internal
export TMUXWEB_TOKEN=...              # 与 .env 里的 TMUX_WEB_API_TOKEN 一致
chmod +x relay_client.py

./relay_client.py peers               # 列出所有主机上的 agent pane、状态和可达性
./relay_client.py ask '%12' '把刚才的部署结论整理成三条'
./relay_client.py ask 'build01~%3' '这版单元测试过了吗？' --timeout 300
./relay_client.py send 'build01~%3' '请先跑一遍单元测试'
```

pane 引用是 `%12`（本地）或 `<host_id>~%3`（远端，`peers` 会输出）。

HTTP 接口（`Authorization: Bearer $TMUX_WEB_API_TOKEN`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/relay/peers` | 所有主机的 pane、provider、`idle`/`busy` 状态 |
| GET | `/api/panes/status` | 精简状态，供前端轮询 |
| POST | `/api/relay/ask` | `{target, question, timeout, force, source}`，同步返回 `reply` |
| POST | `/api/relay/message` | 只发送不等待 |
| GET | `/api/relay/log` | 最近 100 次中继记录（内存环形缓冲） |

中继协议：TmuxWeb 在问题里嵌入一次性标记，要求目标 agent 在回答最后一行输出
`TMUXWEB_REPLY_<token>: "<JSON 字符串>"`，然后按 0.8 秒轮询远端/本地 capture 直到命中或超时（504 会带 pane 尾部内容，便于判断）。目标 pane 正忙时默认拒绝（409），需要 `force=true` 显式覆盖。


## 环境要求

- Linux 或 macOS
- Python 3.11+
- tmux
- OpenSSH 客户端（使用远程主机时还需要 `ssh-keyscan`）
- 可选：Claude Code、Codex、Hermes、Kimi Code CLI

## 安装

```bash
git clone https://github.com/marshaha/TmuxWeb.git
cd TmuxWeb
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

生成登录密码与稳定的 Session Secret：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

将结果分别写入 `.env`：

```dotenv
TMUX_WEB_PASSWORD=替换为长随机密码
TMUX_WEB_SESSION_SECRET=替换为64位十六进制随机值
TMUX_WEB_HOST=127.0.0.1
TMUX_WEB_PORT=8787
```

`.env`、数据库、SSH 凭据和主密钥均被 `.gitignore` 排除。不要提交它们。

## 启动

```bash
chmod +x start_tmux_web.sh
./start_tmux_web.sh
```

浏览器访问 `http://127.0.0.1:8787`。

后台运行示例：

```bash
tmux new-session -d -s tmuxweb "$PWD/start_tmux_web.sh"
tmux capture-pane -p -t tmuxweb -S -100
tmux kill-session -t tmuxweb
```

如果必须监听所有接口，可在 `.env` 中设置：

```dotenv
TMUX_WEB_HOST=0.0.0.0
```

这必须配合防火墙、HTTPS 反向代理或安全隧道。浏览器访问 HTTPS 时，代理到本地源站仍可使用 HTTP。

管理员登录和分享密码按客户端地址限速（默认 60 秒内最多 5 次失败）。Cookie 在 HTTPS 请求下自动带 `Secure`；如果 TLS 代理没有正确传递请求协议，请在 `.env` 设置 `TMUX_WEB_SECURE_COOKIES=true`。

## 添加远程主机

打开 `/hosts`，依次完成：

1. 输入地址、端口和用户名；
2. 选择认证方式：密码（默认掩码，点右侧眼睛才显示）、私钥，或「本机密钥（SSH Agent）」——后者用本机 `~/.ssh` 的默认私钥或 ssh-agent，远端 `~/.ssh/authorized_keys` 里已有对应公钥时可以直接测通，不落任何凭据；注意服务进程要能读到 `SSH_AUTH_SOCK`（systemd 部署时需显式透传）；
3. 测试联通：会自动扫描并记录该主机的 SHA256 host key 指纹，并把指纹显示在表单下方；
4. 添加主机：再次校验 SSH 与远程 `tmux list-panes`，成功后保存。

指纹采用首次连接即记录（TOFU）：之后同一主机返回不同指纹时连接会被直接拒绝，需要到 `/hosts` 删除重加。

密码和私钥不会返回浏览器列表 API。私钥应是完整的 OpenSSH/PEM 私钥，而不是 `ssh-ed25519 AAAA...` 公钥。当前不支持带 passphrase 的加密私钥。

注意：分享一个 shell pane 等于把该 pane 的**读写**终端交给拿到链接的人（可以执行命令）；只想给人看输出时，请在 agent 会话上分享，或分享后尽快撤销/设短有效期。

## 管理远程主机

`/hosts` 页面除了新增，还能直接维护已保存的主机：

- **编辑**：显示名称、主机/IP、端口、用户名、登录方式都能改；凭据留空表示沿用已保存的密码/私钥，填了才替换
- **重新确认指纹**：只有改了主机名或端口才重新扫描并锁定新的 SSH 指纹，其它编辑不动指纹
- **排序**：每行的 ↑ / ↓ 调整顺序，顺序存库（`position` 列，旧库自动迁移），左侧树与 `/api/hosts` 都按这个顺序
- **删除**：连同本地加密凭据一起删除

保存前会先用新配置做一次 SSH + tmux 探测；探测失败就把这次编辑**整体回滚**并返回错误，避免把一台能用的主机改成连不上。
换了密码或密钥但探测失败时，旧凭据仍在库里。

接口：`PUT /api/remote-hosts/{id}`（部分字段，`credential` 省略即不修改）、`POST /api/remote-hosts/reorder`（`{"ids": [...]}`）。

## 移动客户端（Flutter 原生）

`client/` 是 **原生** Flutter 客户端（Android / iOS），不走 WebView，直接调用 HTTP API；网页版继续独立迭代。

- 登录：服务器地址 + 密码（换 `tmux_web_session` cookie），或 `TMUX_WEB_API_TOKEN`；地址记住、密码不落盘
- 主机 / pane 树，标注 provider 与「有结构化记录 / 纯终端」；纯终端按屏幕文本渲染
- 对话里的本地文件链接会被改写：渲染时把 `/abs/path`、`~/path`、`file://…` 和相对路径（按 pane 的工作目录解析）改成 `/api/panes/<pane>/files/raw?path=…`，点击在新标签页打开；文本/图片/PDF 内联预览，其它类型按附件下载（响应带 `CSP: sandbox`，不会执行文件里的脚本）。markdown 里被 `marked` 百分号编码过的中文路径会先解码，`file.md:3685` 这类行号后缀会被去掉；https、mailto、锚点等链接原样保留。
- 点 `.md/.markdown/.mdx/.txt/.log/.json/.yaml/.csv` 链接不跳转，而是在控制台里弹出阅读面板：markdown 走同一套渲染（标题、表格、代码块、mermaid，块级控件照旧），`源码` 按钮可切原文，`新标签打开` 下载/新开，Esc 或点遮罩关闭；面板里的相对链接与图片按被打开文件所在目录解析，嵌套的 md 链接会在同一面板里继续打开。超过 1.2 MB 的 md 默认显示源码，可手动点「渲染」。
- 发送后先本地占位（`发送中`/`已发送`）：回显到达时按文本比对（忽略空白/换行差异），文本被 agent 改写时按顺序配对，保证占位气泡不会滞留在后面；占位气泡插在「你按下发送时最后一条消息」之后，而不是永远排在末尾，所以 agent 抢先回复也不会错位。
- 停止推理：agent 在跑且输入框为空时，发送键变成红色「停止」（方形图标），点击（或输入框里按 Esc）会按 CLI 的习惯发按键打断当前推理：Hermes 用 `Ctrl+Q`（实测 `tmux send-keys C-q` 会中断当前回合，会话保留；`Ctrl+C` 也能中断但 2 秒内再按一次会强制退出，所以不自动发），Claude Code / Codex / Kimi 用 `Esc`；输入框一有文字按钮就恢复成发送箭头（Enter 始终可发送）。接口 `POST /api/panes/{pane}/interrupt`（按 provider 选键）与 `POST /api/panes/{pane}/key`（仅允许 Escape/Enter/Tab/C-c/C-d/C-q/C-u/方向键）。Flutter 客户端同样支持：状态栏进入 thinking 且输入框为空时，发送键变为停止键。
- 推理状态条：输入框正上方常驻一行小字，agent 在跑时显示 `…thinking… 1m38s`（橙色跳动点 + 计时），空闲时显示 `…idle…`；顶栏同步显示 `<provider> · thinking…` 并让状态点橙色呼吸；判断依据是 transcript 最后一条还是 user（本回合未回复），再叠加 pane 屏幕探测（`esc to interrupt`/spinner，覆盖 codex 这种边写边出的情况），两者都结束就回到 `idle`，与侧边栏圆点一致。
- 长会话往回翻：初次读 300 条，滚到顶部会自动再取一倍（300→600→1200…，上限 `TMUX_WEB_TRANSCRIPT_LIMIT_MAX` 默认 3000），顶部也有「加载更早的消息」按钮；加载后保持当前阅读位置不动，取完更早的消息后按钮自动隐藏。
- 顶栏的分享入口是纯图标（右上箭头），没有文字。
- 分享弹窗宽度用 `100vw` 而不是 `100%`，不再随视口/键盘变化而跳动；手机（≤600px）下变成贴底 sheet：占满宽度、圆角只在上方、高度上限 88dvh、表单在内部滚动、操作按钮 sticky 在底部，字段单列排列。HTML 页面统一 `Cache-Control: no-store`，改完刷新即生效。
- 对话：2 秒轮询，发送后乐观占位；每个 pane 的 transcript / 滚动位置 / 草稿缓存在内存里，切回去立刻出画面再后台刷新
- **每条消息「复制全部」**；代码 / mermaid / 表格各自有工具条：复制（表格为 TSV、mermaid 为源码），表格可在「源码 / 渲染」间切换
- 分享链接：路径 + 密码直接打开；`allow_input=false` 的只读分享隐藏输入框并显示只读横幅
- 不含：终端模式、文件浏览器、新建 session、mermaid 图形渲染（这些仍用网页版）

```bash
cd client
flutter pub get
flutter test                 # 44 个单元 / widget 测试
flutter build apk --release --split-per-abi
```

iOS 由 GitHub Actions 在 `macos-latest` 上编译（`.github/workflows/mobile.yml`）：默认未签名 IPA，
配置证书/描述文件/App Store Connect Key 后自动签名并上传 TestFlight。细节见 `client/README.md`。

## 分享会话

管理员可以在任意 pane 上创建分享：agent 会话按结构化消息渲染，纯 shell pane 退化成屏幕文本（可滚动、可继续输入）；分享在 `/shares` 管理：

- 过期时间
- 分享密码
- 续期
- 密码重置或清空
- 提前停止
- 永久删除

创建时可勾选「允许访客继续对话」（默认勾选）。不勾选就是**只读分享**：分享页仍能看到完整历史和后续更新，但输入框被替换为只读说明，`POST /share/<slug>/api/input` 直接返回 403，无法向该 pane 写入任何内容。`/shares` 列表会标注该分享是「可对话」还是「只读分享」。

访客只获得绑定会话的 transcript（以及勾选时可发送消息的权利），不能访问主机树、终端或管理 API。

## MCP 邮箱（可选）

```bash
chmod +x install.sh mailbox.py mcp_server.py
./install.sh
```

它为 Claude Code 和 Codex 安装固定身份的 `hermes-mailbox` MCP 服务，提供：

- `send_message`
- `receive_messages`
- `reply_message`

本地命令示例：

```bash
./mailbox.py send claude '请审查当前方案' --topic review
./mailbox.py inbox
```

## 跨服务器 agent 中继 MCP

TmuxWeb 自带一个 MCP 端点 `POST /mcp`（Streamable HTTP）。客户端只需 URL + token，**不需要** python3、
不需要本仓库的路径，升级只要重启服务端：

```
URL:     http://<host>:8787/mcp
Header:  Authorization: Bearer <TMUX_WEB_API_TOKEN>
```

四个工具，覆盖所有受管主机（含 SSH 远端）：

| 工具 | 作用 |
| --- | --- |
| `list_agents` | 列出所有主机上的 agent pane（target / 主机 / CLI / 空闲或忙碌 / 目录 / 标题） |
| `ask_agent` | 向某个 pane 提问并等待回答（busy 快速失败、可 `force`、timeout 5–600s） |
| `send_agent` | 只输入文字，不等回答 |
| `read_agent` | 读它的屏幕（默认）或结构化 transcript（`transcript=true`） |

安装：

```bash
./install_relay_mcp.sh --dry-run   # 预览（token 打码）
./install_relay_mcp.sh             # 默认 HTTP 模式：claude / codex / kimi 全部走 URL
./install_relay_mcp.sh --stdio     # 兼容模式：客户端各自 spawn python3 mcp_relay.py
./install_relay_mcp.sh --uninstall # 卸载
```

脚本做的事（也可手工）：

```bash
# Claude Code
claude mcp add -s user tmuxweb --transport http http://host:8787/mcp --header "Authorization: Bearer <token>"
# Codex（CLI 只写 url，脚本随后把 http_headers 合并进 ~/.codex/config.toml）
codex mcp add tmuxweb --url http://host:8787/mcp
# Kimi Code（~/.kimi-code/mcp.json）
{"mcpServers": {"tmuxweb": {"url": "http://host:8787/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
# Hermes 的 HTTP 模式会交互式索要 token（无 --yes），所以脚本仍用 stdio：
printf 'y\n' | hermes mcp add tmuxweb --command python3 --env TMUXWEB_URL=http://host:8787 TMUXWEB_TOKEN=<token> --args /path/mcp_relay.py
```

没有 MCP 支持的场景（脚本、shell）可以用 `relay_client.py`，或用 stdio 版 `mcp_relay.py`（`--selftest` 自检）。
内网/Tailscale 部署可以彻底免掉 header：在 `.env` 设 `TMUX_WEB_MCP_AUTH=off`（默认 `on`），`/mcp` 不再校验 token，客户端只填 URL；此时任何能访问该端口的人都能操作 pane，启动日志会打一条警告，`/api/*` 与网页登录仍按原样校验。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

完整的自动化测试、真实 tmux、远程 SSH、移动端和公网反向代理验收方法见 [TESTING.md](TESTING.md)。

## 放在 Cloudflare / 反向代理后面

分享链接**不需要**配置域名：`/api/shares` 返回的是相对路径（`/share/<slug>`），分享页里的请求也都是相对地址，
浏览器会自动用你访问时的域名。所以绑 CF 域名后不用往 `.env` 里写域名。

但走代理后有两件事值得显式设置（都在 `.env`）：

```bash
# 1) 让登录限速按真实客户端计数，并让 cookie 依据 X-Forwarded-Proto 判定 Secure
TMUX_WEB_TRUST_PROXY=1
# 2) 可选：只允许自己的域名访问（127.0.0.1 / localhost 始终放行），防止别人的域名指到这台机器
TMUX_WEB_ALLOWED_HOSTS=chat.example.com
```

- 不开 `TMUX_WEB_TRUST_PROXY` 时，所有请求的客户端地址都是 CF 的出口 IP：登录限速会变成"全站共享 5 次失败"，
  几个人同时试错就会把所有人锁 60 秒；cookie 也可能因为没有 `Secure` 而在 HTTPS 下仍可被明文回传。
- 如果反代终止 TLS 但没传 `X-Forwarded-Proto`，可以退一步直接 `TMUX_WEB_SECURE_COOKIES=true`。
- WebSocket（实时聊天/终端）CF 默认支持；`/ws/*` 不需要额外配置。
- MCP 端点 `POST /mcp` 同理走同一域名，客户端 URL 写 `https://chat.example.com/mcp`。

## 数据与隐私

以下内容不会进入仓库，并且不应手工提交：

- `.env`
- `*.db`、`*.db-wal`、`*.db-shm`
- `.remote_hosts.key`、私钥、密码
- `tmux-panes.json`
- Cookie、日志、终端 capture、AI transcript

结构化 transcript 默认从各 CLI 自己的本地状态读取，不会复制进项目目录。

## 已知限制

- 远端 transcript 不依赖 python3；Hermes 需要 `~/.hermes/sessions/<id>.jsonl` 或 `sqlite3`（PATH、conda 常见路径、`bash -lc` 都会尝试），两者都没有时用 Hermes 自带的 python3 runtime（`~/.hermes/tools/python-*/bin/python3`）通过 stdlib `sqlite3` 导出，仍不行才回退 capture。
- 中继是同步请求：调用方要等目标 agent 回答，最长 300 秒；同一 pane 同时只允许一个中继请求。
- 中继依赖目标 agent 遵守标记协议；不遵守时超时并返回 pane 尾部，便于人工判断。
- 分享要求可稳定识别 provider session；仅 capture 的 pane 不能创建分享。
- 管理员登录目前是单用户共享密码。
- 第三方前端依赖保存在 `web/vendor/`，便于无 CDN 部署；其版权声明保留在文件中。

## 许可证

[MIT](LICENSE)
