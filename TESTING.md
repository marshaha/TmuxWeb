# 测试与验收

本文档覆盖无需真实 AI CLI 的自动测试，以及本地 tmux、远程 SSH、移动端和公网反向代理的手工验收。

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

- FastAPI 登录和 API 权限
- tmux pane 枚举、capture、输入和 WebSocket
- PTY resize/关闭
- Claude transcript 与受限 JSONL 尾部读取
- 分享创建、密码、过期、续期、停止和删除
- 远程主机加密存储、host scoped pane ID 和 SSH 参数构造
- 密码不进入 argv/普通环境变量
- 公钥误当私钥时的拒绝
- 真实 Tab 分隔符的远程 tmux parser 回归测试
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

分别选择正在运行的 Claude Code、Codex 和 Hermes pane：

- Claude：确认 user/assistant 消息顺序、Markdown 表格和代码块正确；compact summary 不显示为普通用户消息。
- Codex：确认 pane title/thread 能定位对应 rollout。
- Hermes：确认按 pane cwd 选择最近活跃 CLI session。
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

## 6. 分享生命周期验收

使用一次性本地 Claude 测试会话：

1. 创建带密码、短有效期的分享。
2. 在无管理员 Cookie 的浏览器/隐私窗口打开链接。
3. 错误密码应返回 401，正确密码后可读 transcript。
4. 访客不能访问 `/api/hosts`、`/hosts`、`/shares`。
5. 重置密码后，旧访客 Cookie 立即失效。
6. 续期后有效期增加。
7. 停止后访客 API 返回 410。
8. 删除后返回 404。

不要把验收消息发送到真实用户会话。

## 7. 移动端/PWA 验收

在 iOS Safari 和 Android Chrome 至少检查：

- 页面打开时不自动弹出键盘。
- 输入框获得焦点后保持可见。
- 移动端 Enter 换行，点击发送按钮才提交。
- IME 组合输入不会误发送。
- 侧栏可折叠，长代码和 Markdown 表格只在气泡内部横向滚动。
- 终端快捷键和方向控制可用。
- 关闭软键盘后终端重新 fit，没有黑色残留区域。
- 安装为 PWA 后可打开；离线缓存中不包含 transcript、API 或登录页面。

## 8. 公网反向代理验收

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

## 9. 发布前隐私检查

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
