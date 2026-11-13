# 子节点：把另一台 TmuxWeb 接进来（单向、只读起步）

一台 TmuxWeb 可以把另一台当成**子节点**：它的窗格会出现在侧栏、能看屏幕、能读对话。
**谁能看到什么、能做什么、多久失效，都由子节点那边签发 auth key 时决定** —— 父节点只拿到
它被允许的那部分，自己放宽不了。

```
子节点 oracle (ddr.167990.xyz)                父节点 localhost / robo
  设置 › 子节点：让别人接入                     设置 › 添加子节点
    · 权限（只能看 / +发按键 / +文件）             · 名字：oracle
    · 有效期（1 小时 … 永久）                     · auth key：粘贴那一串
    · 范围（全部 / 指定编号）                     → 侧栏多一组 oracle#12
    → 生成一串 auth key，列表里随时吊销
```

## 一、在子节点上生成 auth key

设置 › **子节点：让别人接入**

| 选项 | 说明 |
|---|---|
| 权限 | 三个勾：发消息/按键、文件读写、管理会话 + 终端。勾上既代表"这台机器允许"，也直接是签给这把 key 的权限（`只能看` 永远都有） |
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
| POST | `/api/fed/panes/{id}/input` | `{"text","submit"}` → 需要 `input` |
| POST | `/api/fed/panes/{id}/key` | `{"key":"Escape"}` → 需要 `input`，只认白名单键 |
| GET | `/api/fed/files` | 列目录（`path` / `hidden` / `limit`）→ 需要 `files` |
| GET | `/api/fed/files/download` \| `/raw` | 下载 / 内联预览 |
| PUT | `/api/fed/files/upload` | `dir` / `name` / `offset` / `total`，body 是字节 |
| POST | `/api/fed/files/rename` \| `/delete` | 改名 / 删除 |
| POST | `/api/fed/files/zip` | `{"paths","base"}` → zip 字节 |
| POST | `/api/fed/sessions` \| `/sessions/close` | 建 / 关会话 → 需要 `admin` |
| POST | `/api/fed/panes/{id}/close` | 关窗格 → 需要 `admin` |
| WS | `/api/fed/ws/terminal/{id}?token=…` | 终端（PTY）→ 需要 `admin` |
| GET/POST | `/api/fed/policy` | 本机总闸（管理界面用，需登录态） |
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

## 五、现在能做什么

| 作用域 | 能做到 |
|---|---|
| `view` | 侧栏列出窗格（带前缀）、看 capture（可见屏）、读 transcript、`/api/panes/<node~%N>/…` 走同一套接口、MCP `read_agent`（`handle` 就是 `oracle#12`） |
| `input` | 聊天里发消息、发单个按键（Enter / Esc / C-c / 方向键 …）、点交互菜单卡片 —— 等于在对端键盘上打字 |
| `files` | 文件面板列目录、下载（流式）、上传（含分块续传）、改名、删除、打包下载 |
| `admin` | 建 tmux 会话、关窗格 / 关会话、**开终端**（PTY 经 WS 借用对端的窗格） |

父节点这边**不用为子节点改任何用法**：它就是一台主机。聊天、文件面板、`/term?pane=<node~%N>`
全都照旧，底层走 `/api/fed/*`；每个请求都带着 auth key，对端逐条校验作用域、窗格白名单和本机政策。

终端经 Cloudflare 走 WS 会多一层延迟，交互多的话把该子节点设成「直连优先」。

## 五之二、线路

- 线路就两条：节点上存的「优先地址」和「备用地址」，聊天、capture、文件**全都一样**按这个顺序。
  你选「Cloudflare 优先」就一直走 CF，选「直连优先」就一直走直连 —— 没有哪类请求被偷偷改道。
- 备用地址只在优先那条连不上时才会用到。节点的 auth key 只带公网地址时，点一次「测试」会把对端
  报出来的 tailnet 地址记进备用位（不会改你的优先顺序）。
- 大文件如果走 CF：能过，但 CF 等源站响应最多 125 秒（Error 524）、单个请求体 100MB 上限，
  慢链路/源站卡住时才会踩到；嫌慢就点「改直连优先」，或者反过来 —— 两条都由你决定。
- **下载的另一跳**：浏览器 → 本机 这一段，默认也走当前页面（通常是 CF）。想让它改走局域网 /
  Tailscale（不经 CF、不受请求体上限影响），在 设置 › 直连地址 › 「文件下载走直连源站」打开。
  默认关；打开后每次下载会先探测一次直连源站，探不通就原样回落。

## 六、本机政策（这台机器允不允许）

设置 › 子节点：让别人接入 顶部有三个勾：

    □ 发消息 / 按键
    □ 文件读写
    □ 管理会话 + 终端

默认**全关**。关着的时候，连已经签出去的 auth key 也会被拒（403 `子节点没有开放…`）——
auth key 里的 scopes 是"这把钥匙想干什么"，这里才是"这台机器允不允许"。勾上立刻生效，
不需要重新签发；签新 key 时也不允许勾选没打开的能力。

## 七、注意

- **单向**：只由父节点拉子节点，反向不开放；想双向就两边各加一次（各签一串 auth key）。
- **永久要慎用** —— 它不会自己失效。
- auth key 等价于「按这个范围看/操作这些窗格」；`view` 最小。别把它贴进聊天或提交进仓库。
- 经 CF 的单请求有 100 秒 / 100MB 上限：capture / transcript 无所谓，文件以后要分块。
- 子节点如果正在探自己的 SSH 主机，`/api/fed/panes` 会慢十几秒；所以控制台优先吃已有的主机快照，
  「测试」只等 ping（3 秒），不等窗格列表。
- 子节点的 `#N` 变了（对方新开窗口）会在下一次 `/api/hosts` 轮询后反映出来；编号本身在子节点那边稳定。
