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

- 移动客户端（原生 Flutter）：`cd client && flutter test` 全绿（切块顺序与转义、表格解析/TSV、URL 规范化、cookie 登录、pane 引用编码、只读分享 403、聊天渲染与复制、块源码切换、只读隐藏输入框、发送成功/失败、登录校验与记住地址、Token 登录）。`flutter analyze` 无 error。真机冒烟：填 `http://<内网IP>:8899` + 密码 → 主机树 → 打开 `%7` → 消息可复制、表格可切源码、分享链接打开只读分享时没有输入框。构建验收：`flutter build apk --release --split-per-abi` 与 `appbundle` 成功，`apksigner verify --print-certs` 显示 `CN=TmuxWeb`。

- 远端 transcript 增量传输：`cd tests && python -m unittest test_remote_hosts -k incremental` 应通过（第二次轮询只带字节偏移、文件被重写时整窗重取、文件消失时回退定位）；`python -m unittest test_remote_hosts -k gzip` 验证 gzip 负载解码与损坏负载报错。实测一台 300MB 会话的远端主机：首次 `/api/panes/<pane>/transcript` 约 8s，之后 1-2s，响应体 ≤60KB。

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
