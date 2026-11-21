# 子 agent 调用图

agent 之间互相问（MCP 的 `ask_agent`）以前只在屏幕上留一行输出：谁问的、问到哪一步、
答完没有，全靠翻 pane。现在每次 ask 都记一条**父 → 子**的边。

## 记录什么

| 字段 | 含义 |
| --- | --- |
| `parent` | 发起方所在的 pane（MCP 客户端从自己的进程树反查 tmux pane；人/curl 直接调时为空） |
| `child` | 被问的 pane（relay 的 target，可以是 `#12` / `ddr#12`） |
| `question` / `reply` | 全文（各 2 万字符上限） |
| `status` | `running` / `ok` / `timeout` / `error` |
| `duration` | 从开始到结束的秒数 |

## 接口

    GET /api/agents/calls?pane=%4&limit=50    这个 pane 相关的调用（父边或子边都算），新的在前
    GET /api/agents/calls/{id}                单条全文（问 + 答）
    GET /api/agents/tree?pane=%4&depth=3      A→B→C 的树；成环时保留边、不再往下递归

## 界面

顶栏多一个「子 agent」按钮（分享按钮左边），点开是右侧抽屉：

* 每行：状态点（绿=已回复、黄=在等、橙=超时、红=失败）、被问的 pane、状态与耗时、问题首行；
* 点一行展开问/答全文（列表里只带 280 字预览，全文按需拉）；
* 只在这道抽屉打开时轮询（4 秒一次），关掉就不发请求。

## 边界

* 人直接 `curl /api/relay/ask` 时不带 `caller`，记录里 `parent` 为空 —— 图的根节点。
* 连目标都解析不出来（`#9999` 不存在）时不落记录：那时链路上什么都没发生。
* 同一对 pane 的多次调用是多条边，不合并（时间顺序有意义）。
* 数据在 `agent_calls.db`（`.gitignore` 已排除，`TMUX_WEB_CALLS_DB` 可改）。
