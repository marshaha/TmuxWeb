# 子节点：把另一台 TmuxWeb 接进来（单向、只读起步）

一台 TmuxWeb 可以把另一台当成**子节点**：它的窗格会出现在侧栏、能看屏幕、能读对话。
**谁能看到什么、能做什么、多久失效，都由子节点那边签发 auth key 时决定** —— 父节点只拿到
它被允许的那部分，自己放宽不了。

```
子节点 oracle (ddr.167990.xyz)                父节点 localhost / robo
  设置 › 子节点：让别人接入                     设置 › 添加子节点
    · 权限（目前只有"只能看"）                    · 名字：oracle
    · 有效期（1 小时 … 永久）                     · auth key：粘贴那一串
    · 范围（全部 / 指定编号）                     → 侧栏多一组 oracle#12
    → 生成一串 auth key，列表里随时吊销
```

## 一、在子节点上生成 auth key

设置 › **子节点：让别人接入**

| 选项 | 说明 |
|---|---|
| 权限 | 目前只有「只能看」：屏幕 capture + 对话 transcript。发按键 / 文件读写 / 管理会话标着「未开放」 |
| 有效期 | 1 小时 / 1 天 / 7 天 / 30 天 / **永久**（永久不会自己失效，只有吊销能收回） |
| 范围 | 全部窗格，或只给几个（写 `#12, #14` 或 `%12 %14` 都行） |

生成后得到一串，只显示这一次：

```
fed_<id>.<secret>@https://ddr.167990.xyz
                 └── @ 后面是"本机是怎么被访问到的"，父节点不用再问地址
```

列表里每条都在，随时「吊销」；吊销后对方下一次请求就是 401。服务端只存哈希，翻不出明文。

## 二、在父节点上添加子节点

设置 › **添加子节点**：填名字（`oracle`），粘上那串 auth key，点「添加子节点」。

- 名字就是编号前缀：`oracle` → 侧栏显示 `oracle#12`。名字要能用：2–16 位小写字母/数字，以字母开头。
- auth key 带了 `@地址` 就直接用；没带（比如手写 curl）才需要填「地址」。
- **先走哪条线路**：`直连`（tailnet / 局域网，最快）或 `Cloudflare`（公网域名，哪儿都能连）。
  默认直连；选直连但只知道公网地址时，父节点会问一句子节点有没有内网地址并自动改用直连。
  列表里每行都有「改 CF 优先 / 改直连优先」，一键换 —— 只是把两条地址的顺序调过来，
  另一条永远是备用。
- 添加后立刻 ping 一次并报结果：`通了：ddr · 85 个窗格`；连不上就把原因写在黄/红字里。
- 列表里「测试」随时重探；「删除」只删这边的记录 —— 想彻底断开，去子节点那边吊销 auth key。

## 三、编号：`oracle#12`

| 写法 | 含义 |
|---|---|
| `#12` / `12` | 本机（沿用现有的全局编号，永不回收） |
| `oracle#12` / `oracle:12` / `oracle/12` | 子节点自己的第 12 号 |

两边各自维护自己的编号，子节点只负责命名空间 —— 所以不会因为抢号错位。内部 id 是
`<node_id>~%12`（和 SSH 主机的 `<host_id>~%N` 同一个形状），`/api/panes`、URL 编码、
中继全部不用改；界面只是把 `#12` 显示成 `oracle#12`。

## 四、HTTP 接口（给脚本用）

子节点侧：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/fed/ping` | **免鉴权**，回 `{"tmuxweb":true,...}`，用来探测 |
| GET | `/api/fed/panes` | 按 auth key 的 panes 白名单裁剪 |
| GET | `/api/fed/panes/{id}/capture` | 可见屏（`history` 20–500） |
| GET | `/api/fed/panes/{id}/transcript` | 对话视图（`limit` 1–1000） |
| POST | `/api/fed/tokens` | `{"label","scopes","panes","ttl"}`；`ttl=0` 是永久 |
| GET | `/api/fed/tokens` | 列表（不含明文） |
| DELETE | `/api/fed/tokens/{id}` | 立即吊销 |

父节点侧（需要本机登录态或 API token）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/fed/nodes` | `{"label","token","base_url","fallback_url"}`；`token` 可以带 `@地址` |
| GET | `/api/fed/nodes` | 列表（不含 auth key） |
| POST | `/api/fed/nodes/{id}/probe` | 现场 ping + 数窗格 |
| POST | `/api/fed/nodes/{id}/prefer` | `{"prefer":"direct"\|"public"}`，换首选线路 |
| DELETE | `/api/fed/nodes/{id}` | 删除这个子节点 |

## 五、现在能做什么 / 还不能做什么

**能**：侧栏列出子节点的窗格（带前缀）、看 capture、读 transcript、`/api/panes/<node~%N>/…`
走同一套接口、MCP `read_agent` 可用（`handle` 就是 `oracle#12`）。

**还不能**：发消息 / 按键（`input`）、文件读写（`files`）、建会话杀窗格（`admin`）。
`FedTransport` 对写操作直接抛错（"这个节点是只读接进来的"），不会悄悄失败。

## 六、下一阶段（按顺序）

1. `input`：`/api/fed/panes/{id}/input|key` + `FedTransport.send/send_key`，作用域校验。
2. `files`：list / raw / download / upload 代理（经 CF 时分块，单请求别超过 100 秒）。
3. `admin`：建会话 / 杀窗格，默认不给。
4. 终端（WS）不要走 CF：延迟翻倍，要么强制直连，要么继续用 capture 轮询。

## 七、注意

- **单向**：只由父节点拉子节点，反向不开放；想双向就两边各加一次（各签一串 auth key）。
- **永久要慎用** —— 它不会自己失效。
- auth key 等价于「按这个范围看/操作这些窗格」；`view` 最小。别把它贴进聊天或提交进仓库。
- 经 CF 的单请求有 100 秒 / 100MB 上限：capture / transcript 无所谓，文件以后要分块。
- 子节点如果正在探自己的 SSH 主机，`/api/fed/panes` 会慢十几秒；所以控制台优先吃已有的主机快照，
  「测试」只等 ping（3 秒），不等窗格列表。
- 子节点的 `#N` 变了（对方新开窗口）会在下一次 `/api/hosts` 轮询后反映出来；编号本身在子节点那边稳定。
