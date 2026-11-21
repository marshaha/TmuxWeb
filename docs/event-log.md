# 操作日志（先落盘再执行）

「有人往我的 agent 里打了东西，我不知道是谁、发了什么」——以前这个问题没法回答。
现在每个会改状态的 pane 操作都先写一行日志，再去做那件事。

## 记哪些

| kind | 触发点 |
| --- | --- |
| `send` | `POST /api/panes/{pane}/input`（聊天框发消息、排队 worker 投递） |
| `key` | `POST /api/panes/{pane}/key`（Escape 打断等） |
| `exec` | `POST /api/exec`（MCP 跑命令） |
| `session.kill` | `DELETE /api/sessions/{name}` |

每行：`kind / pane / actor（ui 或 mcp）/ status / 请求（截断 4000 字）/ 结果 / 错误 / 耗时`。

## 状态怎么收敛

    running   → 已经开始，还没结果（**崩溃时留在盘上的就是它**）
    ok / error / retrying   终态
    unknown  → sweep() 把「进程重启前就挂着、且超过 5 分钟」的行标成这个

启动时会跑一次 `sweep()`：重启之后还挂着 `running` 的行不可能真在跑，留着只会骗人。

## 接口

    GET /api/events?pane=%4&kind=send&limit=50&full=1
    GET /api/events/inflight        现在还挂着的（多半是上次被杀的）
    GET /api/events/{id}            单条请求/结果全文

## 界面

顶栏「子 agent」抽屉里第二个标签「操作」：每行 = 状态点 + actor + 操作类型 + 状态 + 耗时 + 一行摘要，
点开看请求/结果的完整 JSON。和子 agent 图共用同一个抽屉与轮询。

## 边界

* 日志写失败（磁盘满、库被锁）不会挡住真实操作 —— 可用性优先，代价是那一行缺失；
  这是有意的取舍，不是疏忽。
* `payload` 截断到 4000 字符：够认出是哪条消息，但不是存聊天记录的地方。
* 默认保留 14 天（`EventLog(keep_days=...)` + `prune()`）。
* 数据库 `events.db`（`.gitignore` 已排除；`TMUX_WEB_EVENTS_DB` 可改）。
