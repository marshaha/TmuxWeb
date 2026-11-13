# 联邦：把另一台 TmuxWeb 接进来（单向、只读起步）

一台 TmuxWeb（集成端，例如 robo）可以把另一台（被集成端，例如 oracle 上的
`ddr.167990.xyz`）当作「一台主机」看待：它的 pane 会出现在侧栏、能看屏幕、能读对话。
**权限和可见范围由被集成端签发令牌时决定**，集成端只拿到它被允许的那部分。

```
oracle (被集成端)                         robo (集成端)
  /api/fed/tokens   ← 签发令牌（登录态）     /api/fed/nodes  → 记住节点 + 令牌
  /api/fed/panes    → 它共享出去的 pane      /api/hosts      → 多出一组 "ddr"
  /api/fed/panes/x/capture|transcript       侧栏显示 ddr#12
```

## 一、在被集成端签发令牌

```bash
# 在 oracle 上，用你的登录 cookie（或本地 API token）
POST /api/fed/tokens
{"label":"robo","scopes":["view"],"panes":"all","ttl":86400}
→ {"id":"…","token":"fed_<id>.<secret>","scopes":["view"],"expires_at":…}
```

| 字段 | 说明 |
|---|---|
| `scopes` | `view`（必填）/ `input` / `files` / `admin`。第一阶段只有 `view` 被真正使用 |
| `panes` | `"all"`、`["%12","%14"]`、或 `{"favorite":true,"pane_ids":{…}}` —— 共享范围在这里裁剪 |
| `ttl` | 300 秒 ~ 30 天 |

令牌只存哈希，随时可 `DELETE /api/fed/tokens/{id}` 立即失效；明文只在签发那一次出现。

## 二、在集成端加节点

```bash
POST /api/fed/nodes
{"label":"oracle","prefix":"ddr",
 "base_url":"http://100.123.123.10:8787",        # tailnet / 局域网，优先
 "fallback_url":"https://ddr.167990.xyz",        # CF 域名，坏了再回落
 "token":"fed_…"}
```

- `prefix` 是显示前缀，2–16 位小写字母/数字，全局唯一（`ddr`）。
- 两个地址先试 `base_url`，失败自动换 `fallback_url`；401/403 不再重试（令牌问题换地址也没用）。
- `GET /api/fed/nodes` 列节点（**不含令牌**），`DELETE /api/fed/nodes/{id}` 移除。

## 三、编号：`ddr#12`

| 写法 | 含义 |
|---|---|
| `#12` / `12` | 本机（沿用现有的全局编号，永不回收） |
| `ddr#12` / `ddr:12` / `ddr/12` | 被集成端自己的第 12 号 |

两边各自维护自己的编号，联邦只负责命名空间 —— 所以不会因为抢号而错位。pane 内部 id 是
`<node_id>~%12`（和 SSH 主机的 `<host_id>~%N` 同一个形状），`/api/panes`、URL 编码、
中继全部不用改；界面只是把 `#12` 显示成 `ddr#12`。

## 四、现在能做什么 / 还不能做什么

**能**：侧栏列出节点的 pane（分组名是节点标签）、看 capture（可见屏）、读 transcript
（聊天视图）、`/api/panes/<node~%N>/…` 走同一套接口、MCP `read_agent` 可用。

**还不能**：发消息 / 按键（`input`）、文件读写（`files`）、建/杀会话（`admin`）。
`FedTransport` 对写操作直接抛错（"这个节点是只读接进来的"），而不是悄悄失败。

## 五、下一阶段（按顺序）

1. `input`：`/api/fed/panes/{id}/input|key` + `FedTransport.send/send_key`，作用域校验。
2. `files`：list / raw / download / upload 代理（经 CF 时分块，单请求别超过 100 秒）。
3. `admin`：建会话/杀 pane，默认不给。
4. 终端（WS）不要走 CF：延迟翻倍，要么强制直连，要么继续用 capture 轮询。

## 六、注意

- **单项**：只由集成端拉被集成端，反向不开放；想双向就两边各加一次节点（各签一个令牌）。
- 令牌等价于「按这个范围读写你的 pane」，`view` 最小；别把带 `input` 的令牌贴进聊天。
- 经 CF 的单请求有 100 秒 / 100MB 上限：capture/transcript 无所谓，文件要分块。
- 节点的 `#N` 变化（对方新开窗口）会在下一次 `/api/hosts` 轮询后反映出来；编号本身在被集成端稳定。
