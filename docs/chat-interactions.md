# Chat 里的 harness 交互

「交互通知」显示 harness 最近报告的待办，点「实时作答」可在 Chat 内操作当前 pane 的原生界面。方向键、Tab 和 Enter 均可点击，也可以输入文字。「提问记录」保存会话文件中出现过的问题，保留原来的顺序。重看一条旧问题不会触发按键或重新发起请求。

| Harness | Chat 能显示什么 | 何时清除通知 |
| --- | --- | --- |
| Codex | PermissionRequest 工具审批；rollout 中 request_user_input 及其异步变体的问题和选项进入聊天历史 | 同步提问收到匹配结果或回合结束后清除；异步提问只进历史。审批完成到 Stop 之间可能仍显示审批通知 |
| Claude Code | PermissionRequest、AskUserQuestion 的题干和选项、MCP Elicitation；会话中的 AskUserQuestion 也进入历史 | 匹配的工具结果、ElicitationResult、Stop 或 SessionEnd |
| Kimi Code | 本机新建会话选「结构化 Chat（ACP）」后，工具审批、单选和多选题在 Chat 内提交。普通 TUI 用 PermissionRequest hook 显示通知 | ACP 请求按会话与请求 ID 结束；TUI 通知随 PermissionResult 等结束事件清除 |
| Hermes | human-input hook 的 clarify、approval、sudo。澄清显示题干，sudo 只显示需要到终端输入密码 | human-input resolved 中相同的 request_id |
| Gemini CLI | Notification 的 ToolPermission 提醒 | AfterAgent 或 SessionEnd；工具获准后到回合结束之间可能仍显示提醒 |
| OpenCode | TUI 插件读取当前路由的 permission 和 form 列表，显示每项审批或填表提醒 | 项目从原生列表移除、切换会话，或插件心跳过期 |

从 Codex rollout 推断尚未返回的同步提问时，只有 pane 的 resume 会话 ID 对得上才显示待办。按标题或工作目录找到的记录只进历史。

原生通知按主机、tmux pane ID、pane PID 和 harness 匹配。接收器按会话和请求保存状态。Chat 共享服务器已有的状态轮询。原生事件可以在没有屏幕菜单时显示；操作在 harness 的原生界面完成。OpenCode 插件仅适用于提供下列 TUI 接口的版本。OpenCode 和 Gemini 的完整聊天历史尚无适配器。

已核实的交互默认使用「聊天内显示」。手动「查看交互」默认关闭，可在「设置 → 实验性 → 手动查看交互」开启，开关随账号保存。待答卡片和展开的实时作答区跟在最新消息下方，可直接点选或用键盘作答。开启实验开关后，聊天工具栏会显示位置选择器，可切到「浮窗显示」或选择「仅手动查看」可收起自动卡片，需要时再点「查看交互」。该偏好保存在当前浏览器。自动通知与手动查看共用一张卡片。

Chat 中的「实时终端」默认展开，显示当前 pane 最近 12 行非空输出，保留终端文字的颜色。桌面上点击输出区后，可用物理方向键、数字、Tab、Enter、Esc 和常见编辑键操作当前 pane。Shift+Esc 将焦点移回标题。点标题可收起或展开，收起、暂停或切到后台时停止读取。实验开关开启后，「查看交互」位于终端标题旁，终端片段不可用时，入口回到输入框工具栏。默认的内嵌卡片直接接在输出下方。原生请求和当前画面匹配时显示 ↑、↓ 和 Enter，多题的 Hermes 卡片还可用 ⇤ 和 Tab ⇥ 切题。

实时终端的键盘默认收起。点击左下角的键盘图标，按键向右展开，再点即可收起。单行键盘首屏显示 ↑、↓、←、→、Tab、Enter 和 Esc，向左滑可使用 Shift+Tab、Space、Ctrl+C、Ctrl+D。点击直接向当前 pane 发送对应的终端按键，按键顺序与点击顺序一致。收起实时终端时键盘也会收起。

### 按主机启用

把仓库的 `plugins/agent/hooks/observe.py` 安装到运行 TUI 的用户的 `~/.config/tmuxweb-agent/hooks/observe.py`，文件模式设为 0755。每台主机仅需一份。选择目录中与 harness 匹配的配置片段，合并到已有设置：

| 模板 | 目标 |
| --- | --- |
| claude-interactions.json | ~/.claude/settings.json |
| codex-interactions.json | ~/.codex/hooks.json，按 CLI 的 /hooks 流程信任配置 |
| kimi-interactions.toml | ~/.kimi-code/config.toml |
| hermes-interactions.yaml | ~/.hermes/config.yaml，按 Hermes 的首次使用流程授权 hook |
| gemini-interactions.json | ~/.gemini/settings.json |
| opencode/ | OpenCode v2 TUI 本地插件包，在 cli.json 的 plugins 数组里添加该包的绝对路径 |

localhost 的服务器直接批量读取观察结果。通过 WebSocket agent 接入的主机需要使用仓库附带的 1.2.1 或更新版。纯 SSH 和联邦转发的 pane 仍可手动查看屏幕，结构化提问历史取决于其 transcript 适配器。观察器写入同一用户目录下的 interactions.sqlite3，普通事件一小时过期，OpenCode 心跳 40 秒过期。缺少回调、过期和主机暂时离线都会影响通知的及时性。

Hermes 的原生 clarify hook 会记录当前请求 ID 和按顺序排列的题干。主机上的观察器按 pane、进程和 provider 读取未结束的请求。只有单一待答请求的全部题干与当前 Hermes 画面匹配，卡片才启用选项。点选只改变当前选择，按「确认选择」时服务端重新读取 hook 和当前画面，核对请求、题目和选项版本。↑↓ 选择，←→ 切题，也可用 Tab 移到确认按钮。出现另一项审批、请求结束或更换进程后，原卡片的提交会被拒绝。

没有 hook、题干被截断或只有屏幕文字时，「查看交互」可核对原文，「实时作答」可操作原生终端。Codex 有明确会话归属的待答记录、其他 harness 的原生事件均可作为只读通知。旧实验开关和以前保存的应答规则不会触发按键。

Codex 命令审批在「查看交互」中显示待执行命令、仅批准一次、记住前缀（如有）和拒绝。前缀快捷键折到下一行也可识别。Codex 出现完整的命令授权画面时，Chat 显示命令和「在终端审批」按钮，点它会展开实时终端和按键。进度栏显示「等待命令审批」。通过 SSH 管理的窗格会在所属主机查询 hook。命令审批也支持从本机 Codex app-server 读取待处理请求，联邦由窗格所属服务器核对。同一线程、本轮请求与当前终端命令匹配后，Chat 显示命令、工作目录和原因，可改选后提交一次性批准或拒绝。持久授权在原生终端处理。收到原生权限请求时卡片自动出现；手动入口开启且仅有屏幕文字时，展开的实时终端识别到完整菜单后，实时终端标题旁的入口会变为「查看命令审批」，可手动查看。更新后的 Codex 观察器会为每次审批回调记录独立标识。只有一个待处理回调，且其命令摘要与屏幕一致时，选「仅批准这一次」或「拒绝」并点「确认选择」会重新核验当前审批，再发对应快捷键。记住前缀、旧版观察器或只有屏幕预览的选项标有「实时确认」，点「确认选择」后复核并进入 Chat 内原生审批。实时终端检测到菜单结束时收起对应卡片。

### 模型选择

打开 Chat 模型菜单时才读取目录。Codex 使用该会话 rollout 所在主机和目录对应的 models_cache.json，只展示 visibility 为 list 的模型。缓存是建议列表，账号权限、CLI profile 或服务端策略可能已变化。菜单也能打开该会话的原生 /model 选择器。缺少目录或对端不支持读取时，直接提供原生入口。Claude 的快捷项标为常用别名，Kimi 使用原生菜单，Hermes 展示对应主机的 provider 返回列表。Codex 当前模型来自最近的 turn_context；发出 /model 命令后，等会话记录更新再改变标记。

### 上游接口

- [Codex 原生模型选择器](https://developers.openai.com/codex/cli/slash-commands/) 和 [App Server](https://developers.openai.com/codex/app-server/)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks) 和 [Kimi Code hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html)
- [Hermes 人工输入事件](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) 和 [Gemini CLI hooks](https://geminicli.com/docs/hooks/reference/)
- [OpenCode TUI 的会话、权限与表单接口](https://opencode.ai/docs/build/plugins/cli/)

### Hermes 原生表单

选中 Hermes 会话时，Chat 在对话末尾以 Hermes 消息显示 gateway 当前开放的问题。等候出题期间可以留在 Chat。出题时实时终端自动收起，仍可手动展开。选好每道题后点「提交全部答案」，也支持多选和「其他」。同一浏览器标签页刷新可恢复未提交的选择。临时草稿按 pane 和请求版本隔离。另一个浏览器或原生终端先答时，Hermes 会关闭同一个请求。普通命令审批提供「仅允许这一次」和「拒绝」。

需要支持共享 gateway 与 terminal breadcrumb 的 Hermes CLI。Linux 主机用当前前台 Hermes 进程、TTY、进程启动时间和该终端的 Hermes 会话记录确认归属。有 hook 时还会核对其 session 和问题。服务只连接正在运行的本机 gateway。终端画面只能提示服务复查。旧版 Hermes、身份不匹配、密码、持久授权和文件编辑审批均可通过实时终端处理。

同一 pane、请求版本与答案重试可取回同一回执。主机端 SQLite 只保存答案摘要和结果，跨进程共享；不同答案无法覆盖先提交的决定。连接中断而结果尚未核实时会提示查看终端。

### 接入新的原生表单

每个 harness 在 `providers/<name>.py` 中实现可选的 `native_form`。读取返回由实际请求生成的 revision 和问题。提交时适配器重新校验 pane 归属、请求身份和选项，并处理重复提交。支持托管主机时显式声明 `REMOTE_NATIVE_FORM`。浏览器在 `harness-interactions.js` 选择读取适配器，`native-form.js` 统一管理选项草稿和确认。联邦请求由所属节点校验输入权限。侧栏中的 `capabilities.native_form` 区分 owner、terminal 和只读的 view。


### Kimi 结构化 Chat

在运行 Kimi 的 Linux 节点打开「新建 tmux 会话」，选择 Kimi，再将「Kimi 界面」设为「结构化 Chat（ACP）」。需要已登录并支持 `kimi acp` 的 CLI，启动参数留空。既有 TUI 会话继续沿用终端。联邦端可以通过已有的输入授权操作所属节点上的 ACP 会话。

`kimi_acp.py` 持有子进程的 stdio。页面仅提交当前请求的 revision 和选项索引，所属进程将其映射成 Kimi 给出的 optionId 或问题值。新 Chat 和联邦端复用同一个请求。退出该 ACP 主进程会结束本次实时连接，已保存的会话记录仍在 Kimi 的存储中。

### 同时处理多个请求（v0.26.0）

Chat 的请求选择器和「等你回答」使用同一份实时队列。选择请求、勾选选项后，点击提交才会回传。另一设备已处理的请求会关闭。联邦连接需要所属节点授予该 pane 的输入权限。

- **Codex** 将 pane 的审批 hook 与 App Server 当前 thread、turn 和待处理请求匹配。命令卡支持本次允许和拒绝。文件卡先列全部路径（包括移动目标），随后展示补丁。审批上下文、请求身份或完整补丁无法核对时，请打开实时终端。
- **Claude Code** 在所属 Linux 节点新建会话时，选择 Claude Code 和「结构化 Chat」。MCP 的 `create_session` 可传 `agent="claude", structured_chat=true`。该进程通过 CLI 的双向 stdio 接收权限回调，支持工具审批、带选项的提问和附带计划正文的退出计划模式请求。回调包含子 agent ID 时，卡片会显示来源。现有终端会话继续用原入口。
- **Hermes** 由前台 CLI 和所属网关核对待办。本地和 SSH 读取均按会话及网关 epoch 保存事件游标。回放位置只用于省去已读事件，每次都从网关核对未完成的请求。

使用本机回环模型和独立 HOME，可以用 `python3 tests/codex_cli_smoke.py /absolute/path/to/codex` 验证两个实际的 App Server 审批请求。测试观察器提供 pane 绑定，Codex 产生待审批命令和两文件补丁，并接收拒绝决定。

开发回归可在隔离副本运行 `python3 tests/claude_cli_smoke.py /absolute/path/to/claude`。它使用独立 tmux socket、临时 HOME 和本机回环模型服务，验证真实 Claude Code 进程收到多选回答。
