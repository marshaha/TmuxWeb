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

侧边栏右边缘可以拖动改宽度（200–640px，双击边缘恢复默认），宽度记在 `localStorage` 里，控制台与主页各记各的；侧边栏变窄时顶部的图标按钮会自动换行（放不下才横向滚动），不会被裁掉点不到；手机上侧边栏是抽屉，宽度固定，不受影响。

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

## 性能：选中「刷屏」pane 不会把浏览器拖死

日志型 pane（uvicorn、`tail -f`、`make`、`while true; do echo; done`）每秒能产出成千上万行。之前选中这类 pane 会重建整块屏幕 DOM、反复重排字体、并让 xterm 的写入队列无限增长，Chrome 直接卡死。现在两边都做了限制：

- 实时屏幕流（`/ws/panes/{pane}` 聊天模式的 capture）第一帧带 1500 行，之后只回传最近 800 行；单帧超过 192KB 只保留尾部并标注「输出过快，已跳过前面 N 字节」；空闲时轮询间隔从 0.7s 退避到 3s
- 前端最多每 500ms 重绘一次、只渲染最近 600 行、只保留最后一条待绘帧（中间帧丢弃）；字体自适应当容器宽度不变时不再重排
- 终端模式（xterm）：写入按 rAF 合并；积压超过 1.5MB 时丢弃最早的数据并插入一行提示，而不是让内存无限增长
- 顶栏新增 **暂停 / 继续实时刷新** 按钮（⏸/▶）：暂停后关闭实时连接，屏幕保持不动，适合看日志时临时冻结
- `/api/hosts` 5 秒快照，`?refresh=1` 才真正重新探测远端；侧边栏刷新从每 5 秒改为每 20 秒，且内容没变时不再重建 DOM（顺带修掉了点击被吞、滚动位置被重置的问题）
- 相关环境变量：`TMUX_WEB_STREAM_LINES`、`TMUX_WEB_STREAM_FIRST_LINES`、`TMUX_WEB_STREAM_MAX_BYTES`、`TMUX_WEB_HOSTS_TTL`

实测（140×40 的 `while true; do echo; done` pane，10 秒窗口）：聊天模式主线程耗时 0.69s → **0.09s**，DOM 文本 363k → 55k 字符；暂停后 6 秒仅 0.02s。

### 状态扫描与 transcript 轮询

- busy/idle 扫描原来**每个 pane 一次 SSH capture**：69 个 pane（macmini 35、oracle 15）要 69 次往返，一次 8.7–14.4s。
  现在按主机批量：`capture_many()` 用一条命令取回该主机所有 pane 的尾部，再在本地 `classify()`，并且优先复用 `/api/hosts` 刚探测过的 pane 列表
  （不再重复 `list-panes`）。实测 **2.1–3.2s**（冷启动首个扫描仍是 ~8s，因为要建立 4 条 SSH 连接）。
- transcript 轮询的缓存 TTL 跟着链路走：最后一次拉取耗时多少，就缓存多久（下限 2s、上限 10s）。
  所以弱网主机不会每秒发一次 SSH——oracle 的连续 8 次轮询实测 0.36–2.0s（首次 10.6s），之前每次 1.4–7.6s。
- 聊天视图改成**增量渲染**：新消息只追加自己那一个气泡（连同 markdown/代码块增强），不再把整段对话重建一遍；
  只有「加载更早」、发送中的乐观气泡、或 DOM 与消息列表不一致时才回退到全量重绘。
- 页面隐藏（切到别的标签）时暂停 transcript 轮询，回到前台立刻补一次。

实测：158 条消息的会话，闲置 10 秒主线程 0.047s、60fps；`/api/panes/status` 命中缓存 0.01s、缓存过期后 2.16s 内给新值。

## 跳板机（jump host）与让 agent 自己添加服务器

添加主机时可以选一个已保存的主机作为**跳板机**（`jump_host_id`），连接会先到跳板机再跃到目标：

- 实现方式是 `ProxyCommand` + 内层 `ssh -W %h:%p`，所以**每一跳可以有不同的凭据**（跳板机用自己的密码/私钥，目标用自己的）；密码场景下会生成一个 askpass 脚本，按提示里的主机名区分返回哪一跳的密码
- 跳板机的主机指纹和普通主机一样固定：已记录就 `StrictHostKeyChecking=yes`，没有就首次连通自动记录
- **支持多级跳板**（A → B → C，最多 `TMUX_WEB_JUMP_MAX_DEPTH` 层，默认 5）：每一跳都用自己的凭据，嵌套成 `ProxyCommand=ssh -W %h:%p ... -o ProxyCommand=ssh -W %h:%p ...`；不允许指向自己、指向不存在的主机、或形成环（保存时返回 422）
- 删除某台跳板机时，经由它的主机自动回退为直连，不会留下悬空引用
- 主机列表里会显示完整的「经由 B → A」链路；编辑弹窗里可以改跳板机；`GET /api/remote-hosts` 也返回 `jump_chain` 和 `jump_path`
- **跳板机不必先添加成主机**：下拉选一台已保存的主机，或者直接在下面那行填 `user@host:port`（也可以只填 `host`）。手填的跳板机复用目标机自己的凭据（同一把 key / 同一个密码），所以密码只存一份；如果跳板机需要**不同**的凭据，就把它单独添加成主机再用下拉引用。两种方式只能选一种，同时填会返回 422

### HTTP API

```bash
# 列表（包含 jump_host_id）
curl -H "Authorization: Bearer $TMUX_WEB_API_TOKEN" https://your-host/api/remote-hosts

# 添加一台经由跳板机的目标机（agent 认证，不需要传密码）
curl -X POST https://your-host/api/remote-hosts -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "name": "inner-box", "hostname": "10.0.0.20", "username": "ubuntu", "port": 22,
    "auth_type": "agent", "jump_host_id": "<跳板机的 id>", "verify": false
  }'

# 或者不保存跳板机，直接写 user@host:port（复用目标机的凭据）
curl -X POST https://your-host/api/remote-hosts -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{
    "name": "inner-box", "hostname": "10.0.0.20", "username": "ubuntu",
    "auth_type": "agent", "jump_spec": "ops@bastion.example:2222", "verify": false
  }'
```

### MCP

| 工具 | 作用 |
| --- | --- |
| `list_remote_hosts` | 列出已保存主机（id/名称/地址/认证方式/跳板机），不返回凭据 |
| `add_remote_host` | 添加主机：`name`、`hostname`、`username`、`port`、`auth_type`（默认 `agent`）、`credential`、`jump_host`（已保存跳板机的 id）**或** `jump`（`user@host:port`，不必先添加）、`verify` |
| `remove_remote_host` | 按 id 删除主机 |

`auth_type=agent` 用本机 ssh-agent 密钥，agent 无需传任何密码；`verify=false` 时即使暂时连不上也会保存，等下次连通再固定指纹。


## 主页：收藏、看板与搜索

（主页右上角有「主机 / 分享 / 刷新」，回到控制台的入口在左侧会话树顶部。）控制台左侧每个 pane 右侧有一个五角星：空心 = 未收藏，实心 = 已收藏（点一下切换）。收藏的会话会出现在 `/home` 主页。

主页（`/home`）左侧保留与控制台一致的**可折叠会话树**（主机 → session → window → pane，带 busy/idle 状态点与收藏星标；宽屏时可折叠、折叠状态记在 `localStorage`，窄屏时变成抽屉 + 遮罩）。右侧是看板：

- **收藏卡片**：卡片名可改（铅笔图标 → 输入新名字，或「恢复默认」）；默认名是 **session · window · 主机**（tmux 的 pane title 常常只是主机名，所以不再直接用它）；卡片右上角有**完整预览**按钮：电脑上从右侧滑出抽屉，里面是完整的对话页（markdown/代码/表格、可继续发送、可停止、每 2 秒刷新），手机等窄屏则直接跳到 `/preview?pane=…`，返回手势/按钮回到主页；**鼠标悬停卡片会浮出最后一条 agent 回复的完整内容**（浮层可直接用鼠标滚轮滚动）（若最新一条是用户消息，则自动往上找最近的一条 AI 回复，最多 6000 字）；另有状态徽标（busy / idle / 离线；busy 且在等回复时显示「思考中…」并跳动）、provider、标题、路径、**最近一条对话预览**、pane 引用；点卡片直接跳回控制台并选中该 pane（`/?pane=<pane>`）
- **看板卡片**：agent 通过 MCP 工具 `publish_home_card` 推上来的结论/告警/链接，可在主页删除
- **「抓屏」和「终端」是两件事**：聊天模式兜底时用 `tmux capture-pane` 取一份**文本快照**（把整屏文字 + ANSI 转成 HTML 塞进页面），它没有终端语义、每帧整屏重建，全屏 TUI 会卡；「终端」模式是真正的 xterm + PTY（增量写 VT 序列，只重绘变化的格子）。所以 agent 会话读不到 transcript 时不再抓屏，而是给一个「用终端打开（xterm）」按钮。
- **tmux 路径只探测一次**：探测到的绝对路径（例如 macmini 的 `/opt/homebrew/bin/tmux`）会存进主机记录（`remote_hosts.tmux_path`），之后所有命令、以及终端模式的 PTY attach 都直接用它——之前 macmini 切终端会报 `zsh:1:1 command not found: tmux`，就是因为 attach 那条命令没带上解析出的路径。主机管理页会显示这个路径，没有时提示「tmux 路径待首次连通时记录」。
- **传输窗口自动适应**：不需要配参数——每次「发现会话」的拉取耗时 >8s 就把该主机的 tail 窗口减半（4MB→1MB 下限），耗时 <2.5s 就加倍回 4MB；实测 oracle（弱网）从 14–30s 降到 5–7s，消息条数不变。
- **慢网络**：远端 transcript 单次拉取有超时（`TMUX_WEB_TRANSCRIPT_TIMEOUT`，默认 30s；超时提示是「网络较慢」而不是解析失败），tmux 路径探测也限时（`TMUX_WEB_PROBE_TIMEOUT`，默认 10s，超时就退回裸 `tmux` 而不是报错），单次传输字节可调（`TMUX_WEB_REMOTE_PAYLOAD_BYTES`）。实测 oracle（单条 ssh 命令 2–4s）：首次发现会话 ~29s，之后走增量探测 0.9–7.6s。
- **agent 会话不会退化成刷屏终端**：transcript 读不到内容时（会话刚开、或解析不出来），聊天区显示说明 + 「查看原始屏幕」按钮并自动重试，不再自动切去流式抓屏（Kimi/Claude 这类全屏 TUI 每秒重绘整屏，600 行 × 7000 个节点会把浏览器卡死）。shell pane 仍然照旧显示屏幕。
- **Kimi 会话挑选**：同一个目录/标题可能有多个 kimi session，只挑 `updatedAt` 最新的那个（旧的往往是中断的回合，里面没有消息）。
- **切回主页不等待**：`/home` 先用 `localStorage` 里的上一份看板（标「显示缓存，正在更新…」）立刻画出卡片和会话树，再拉最新数据替换；没有缓存就显示骨架屏（统计/卡片/树的占位），不会空白。接口侧 `/api/home` 最多等 1.2 秒——收藏卡片的实时状态和最后一条消息要按 pane 走 SSH，慢的话先返回占位（`pending`），后台读完写进 25 秒缓存，下一次轮询补上。
- **对话右上角的五角星**：收藏/取消收藏当前打开的 pane，状态与侧边栏列表里的星标、主页卡片联动；对话预览页（`/preview`）头部也有同一个按钮
- **搜索**：先匹配「服务器 / tmux session / pane 名」，再搜各 agent **最近的对话内容**，最后是看板卡片；结果按这四组分开显示并高亮命中片段
  - 支持**正则**：查询里有正则元字符（`. * + ? [ ] ( ) { } | ^ $ \`）就按正则跑，否则按字面匹配；正则写错会自动退回字面匹配（结果页会标出来）
  - 例：`^codex` 只看以 codex 开头的 session，`claude|codex` 两者都算，`eth.*iter` 匹配中间任意内容
  - 对话是逐个 pane 读的，所以有 4 秒预算 + 25 秒缓存；没扫完会标「对话还在扫，结果可能不全」
  - 顶栏只留搜索框；「远程主机 / 管理分享 / 刷新」和控制台一样放在侧边栏顶部（图标按钮）
- **统计条**：panes / busy / idle / 收藏数 / 看板卡片数
- 每 15 秒自动刷新；手机上单列布局

### 让 agent 自己往主页贴东西

MCP 新增三个工具（stdio 与 HTTP `/mcp` 都支持）：

| 工具 | 作用 |
| --- | --- |
| `publish_home_card` | `title`（必填）、`body`、`pane`、`kind`（note/result/alert/link）→ 在主页生成一张卡片 |
| `list_home` | 读取主页：收藏（含状态与最后一条消息）+ 卡片 |
| `remove_home_card` | 按 id 删除自己发布的卡片 |

```
publish_home_card(title="eth 任务已收敛", body="ITER5 结论已写入 notes，等 review", kind="result")
```

数据存在 `TMUX_WEB_HOME_DB`（默认 `home.db`），收藏与卡片都只写这一张表，不依赖 tmux。

## 移动客户端（Flutter 原生）

`client/` 是 **原生** Flutter 客户端（Android / iOS），不走 WebView，直接调用 HTTP API；网页版继续独立迭代。

- 登录：服务器地址 + 密码（换 `tmux_web_session` cookie），或 `TMUX_WEB_API_TOKEN`；地址记住、密码不落盘
- 侧边栏树：主机 → session → window → pane 四级展开（展开状态会记住），pane 前有 idle/busy 圆点（来自 `/api/panes/status`）；**每台主机行右侧有独立的刷新按钮**（`/api/hosts?refresh=1&host=<id>`，只重新列这一台，实测 macmini 0.14s / oracle 0.37s，不会像整表刷新那样挨个探测所有主机），控制台和主页的树里都有
- 服务器管理：列表 / 新增 / 编辑 / 删除远程主机，可扫描并固定 SSH 指纹，带「显示凭据」与「不校验连接直接保存」
- 对话：结构化 transcript（Claude/Codex/Hermes/Kimi），每条消息复制、代码/mermaid/表格块级源码切换与复制
- 发送：Enter 发送、Shift+Enter 换行；agent 在跑且输入框为空时发送键变红色停止键（Hermes 发 Ctrl+Q，Claude/Codex/Kimi 发 Esc）
- 终端：调 `/ws/terminal/{pane}` 的真实 PTY（xterm 渲染），本地与 SSH pane 都可用，断线自动重连
- 文件面板：浏览 pane 目录、点文件预览（markdown/图片/文本）、下载到设备（存到应用文档目录）、上传、打包当前目录 zip
- 会话缓存：每个 pane 的 transcript、滚动位置、草稿都留在内存（最多 8 个），切回去立刻出画面再后台刷新
- 分享：输入分享路径（可带密码）以只读/可对话方式查看

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
