# 远程调试接口（给 agent 用）

给另一个 agent（比如 muse）远程调试 TmuxWeb 时，**不要给交互 shell**：抢一个 pane、敲命令、
读屏、再猜输出，一轮就要几十秒。这里的接口一次调用换一次结果。

- 基址：`https://tmux.122990.xyz`（局域网可换 `http://10.168.236.31:8787`）
- 鉴权：`X-TmuxWeb-Token: <token>`（也接受 `Authorization: Bearer <token>`）
- token 在服务器的 `~/workspace/TmuxWeb/.env` → `TMUX_WEB_API_TOKEN`，**不要贴进聊天或提交**

```bash
export TW=https://tmux.122990.xyz
export TK=...        # TMUX_WEB_API_TOKEN
```

## 1. 跑命令（主力）

```bash
curl -s -X POST $TW/api/exec -H "X-TmuxWeb-Token: $TK" -H 'Content-Type: application/json' \
  -d '{"command":"cd ~/workspace/TmuxWeb && ./.venv/bin/python -m unittest discover -s tests -q 2>&1 | tail -5",
      "timeout":300}'
```

```json
{"host":"local","cwd":"/home/congyong/workspace/TmuxWeb","exit":0,
 "stdout":"Ran 631 tests in 46s\nOK\n","stderr":"","truncated":false,"duration":46.2}
```

| 字段 | 说明 |
|---|---|
| `command` | 走 `bash -lc`，可以带管道、`&&` |
| `cwd` | 工作目录，默认服务器仓库根 |
| `timeout` | 秒，默认 60，上限 600；超时返回 `exit: 124` 且 `stderr` 写 timeout |
| `max_output` | 单流截断字节，默认 200000；截断时 `truncated: true` |
| `host` | 省略/`local` = 本机；否则填 `/api/hosts` 里的 id 或名字（走已配置的 SSH/跳板机） |

重启线上服务（守护进程 10 秒内拉起）：

```bash
curl -s -X POST $TW/api/exec -H "X-TmuxWeb-Token: $TK" -H 'Content-Type: application/json' \
  -d '{"command":"kill $(ps -eo pid,args | grep -E \"[u]vicorn tmux_web\" | awk \"{print \\$1}\" | head -1)"}'
```

看日志走同一条：`{"command":"tail -50 ~/workspace/TmuxWeb/logs/service.log"}`。

## 2. 文件

```bash
P=%28                                          # pane id，或 #12 这种短编号
ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$P")

# 列目录
curl -s -H "X-TmuxWeb-Token: $TK" "$TW/api/panes/$ENC/files?path=/home/congyong/workspace/TmuxWeb"
# 读文本/图片（inline）
curl -s -H "X-TmuxWeb-Token: $TK" "$TW/api/panes/$ENC/files/raw?path=/path/to/file.md"
# 下载（附件）
curl -s -H "X-TmuxWeb-Token: $TK" -o out.bin "$TW/api/panes/$ENC/files/download?path=/path/to/big.bin"
# 上传（按块，409 带 offset 可续传；1MB 一块最稳）
curl -s -X PUT -H "X-TmuxWeb-Token: $TK" \
  "$TW/api/panes/$ENC/files/upload?name=a.txt&dir=/tmp&offset=0&total=$(stat -c%s a.txt)" --data-binary @a.txt
# 打包（服务端 tar，本地不用装）
curl -s -X POST -H "X-TmuxWeb-Token: $TK" -H 'Content-Type: application/json' \
  -d '{"paths":["/tmp/a.txt","/tmp/b.txt"],"base":"/tmp"}' "$TW/api/panes/$ENC/files/zip" -o files.zip
```

## 3. tmux pane

```bash
curl -s -H "X-TmuxWeb-Token: $TK" "$TW/api/panes"                    # 全部 pane（含 #编号）
curl -s -H "X-TmuxWeb-Token: $TK" "$TW/api/panes/$ENC/capture?history=40"   # 屏幕文本
curl -s -X POST -H "X-TmuxWeb-Token: $TK" -H 'Content-Type: application/json' \
  -d '{"text":"echo hi","submit":true}' "$TW/api/panes/$ENC/input"
curl -s -X POST -H "X-TmuxWeb-Token: $TK" -H 'Content-Type: application/json' \
  -d '{"key":"C-c"}' "$TW/api/panes/$ENC/key"      # ALLOWED_KEYS 里的单键，如 Escape / C-c / Enter
```

只想跑一次性命令就用第 1 节；pane 接口留给"必须看交互界面"的场景。

## 4. MCP（agent 原生调用）

TmuxWeb 自带 `/mcp`，MCPHub（`http://100.123.123.3:3300/mcp`）也把它聚合了。
新工具 **`run_command`** 就是第 1 节的 exec，参数一致：

```json
{"name":"run_command","arguments":{"command":"git -C ~/workspace/TmuxWeb status --short","timeout":60}}
```

其余工具：`list_agents` / `ask_agent` / `send_agent` / `read_agent` / `send_file` /
`create_session` / `kill_session` / `list_remote_hosts` / … （`tools/list` 为准）

## 5. 安全

- 这个 token 等价于 **congyong 的 shell**（他在 docker 组）。只发给可信 agent，别写进仓库。
- 不想要 exec：`.env` 里 `TMUX_WEB_EXEC=off`，重启后 `/api/exec` 返回 403（其余接口照常）。
- 轮换 token：改 `.env` 的 `TMUX_WEB_API_TOKEN` 后重启服务；旧 token 立刻失效。
- 需要临时给人看屏幕/敲键盘，才用 `/share/<slug>`（带密码、有到期时间、可 `/stop`）。
