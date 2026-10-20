# TmuxWeb

[English](README_EN.md) · [测试与验收](TESTING.md) · [Testing](TESTING_EN.md)

TmuxWeb 是一个自托管的 tmux 与 AI CLI 会话管理中心。它把本机及 SSH 远程主机上的 tmux pane 组织为 `主机 → session → window → pane`，并提供 ChatGPT 风格聊天、真实 PTY 终端、结构化会话记录和受控分享。

> 安全提醒：TmuxWeb 能向 tmux pane 输入文字并提供交互终端，等同于访问运行服务的系统账户。不要直接暴露到公网；请使用 HTTPS、访问控制、强密码和防火墙。

## 功能

- **多主机管理**：一个本地服务管理 localhost 和 SSH 远程主机；远端只需 SSH 与 tmux，不需安装守护进程。
- **tmux 树导航**：按主机、session、window、pane 展示；展开状态保存在浏览器本地。
- **AI 聊天视图**：Claude Code、Codex、Hermes 优先读取结构化 transcript；无法解析时回退 ANSI `capture-pane`。
- **真实终端**：xterm.js + PTY attach，支持输入、resize、ANSI 和移动端快捷键。
- **安全发送**：文字经 tmux buffer 原样粘贴，不拼接 shell 命令；提交时处理旧草稿和 bracketed paste。
- **远程连接**：支持密码和私钥；固定 SSH host key 指纹；凭据使用独立 Fernet 主密钥加密保存。
- **会话分享**：可设置 slug、密码、有效期；支持续期、重置密码、停止和删除。
- **PWA/移动端**：适配软键盘、安全区和窄屏；Service Worker 不缓存 API、transcript 或登录状态。
- **受控 MCP 邮箱**：可选的 Claude Code、Codex、Hermes 文本消息中继，不开放任意 shell/文件能力。

## 架构与边界

- 聊天模式不创建长期 `tmux attach`，避免多个客户端参与 tmux 尺寸协商。
- 只有终端模式使用 PTY attach；WebSocket 断开后释放。
- 远程结构化 transcript 尚未实现时，会明确回退到远程 `capture-pane`。
- 远端 pane ID 带主机作用域，避免不同 tmux server 的 `%0` 冲突。
- 当前管理登录是单用户模型；多用户场景还需要用户级主机授权与审计。

## 环境要求

- Linux 或 macOS
- Python 3.11+
- tmux
- OpenSSH 客户端（使用远程主机时还需要 `ssh-keyscan`）
- 可选：Claude Code、Codex、Hermes CLI

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
2. 选择密码或私钥认证；
3. 扫描并通过可信渠道核对 SHA256 host key 指纹；
4. 勾选确认；
5. 测试 SSH 与远程 `tmux list-panes`；
6. 测试成功后保存。

密码和私钥不会返回浏览器列表 API。私钥应是完整的 OpenSSH/PEM 私钥，而不是 `ssh-ed25519 AAAA...` 公钥。当前不支持带 passphrase 的加密私钥。

## 分享会话

管理员在 Claude 结构化会话中创建分享，并可在 `/shares` 管理：

- 过期时间
- 分享密码
- 续期
- 密码重置或清空
- 提前停止
- 永久删除

访客只获得绑定会话的 transcript 和消息发送权限，不能访问主机树、终端或管理 API。

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

## 测试

```bash
python3 -m unittest discover -s tests -v
```

完整的自动化测试、真实 tmux、远程 SSH、移动端和公网反向代理验收方法见 [TESTING.md](TESTING.md)。

## 数据与隐私

以下内容不会进入仓库，并且不应手工提交：

- `.env`
- `*.db`、`*.db-wal`、`*.db-shm`
- `.remote_hosts.key`、私钥、密码
- `tmux-panes.json`
- Cookie、日志、终端 capture、AI transcript

结构化 transcript 默认从各 CLI 自己的本地状态读取，不会复制进项目目录。

## 已知限制

- 远程 pane 当前使用 capture/input；远程结构化 transcript 尚未实现。
- 分享要求可稳定识别本地 provider session；不要给仅 capture 的远程 pane 创建分享。
- 管理员登录目前是单用户共享密码。
- 第三方前端依赖保存在 `web/vendor/`，便于无 CDN 部署；其版权声明保留在文件中。

## 许可证

[MIT](LICENSE)
