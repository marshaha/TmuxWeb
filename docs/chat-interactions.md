# Chat 里的 harness 交互

「交互通知」显示 harness 最近报告的待办，点「实时作答」可在 Chat 内操作当前 pane 的原生界面。方向键、Tab 和 Enter 均可点击，也可以输入文字。「提问记录」保存会话文件中出现过的问题，保留原来的顺序。重看一条旧问题不会触发按键或重新发起请求。

| Harness | Chat 能显示什么 | 何时清除通知 |
| --- | --- | --- |
| Codex | PermissionRequest 工具审批；rollout 中 request_user_input 及其异步变体的问题和选项进入聊天历史 | 同步提问收到匹配结果或回合结束后清除；异步提问只进历史。审批完成到 Stop 之间可能仍显示审批通知 |
| Claude Code | PermissionRequest、AskUserQuestion 的题干和选项、MCP Elicitation；会话中的 AskUserQuestion 也进入历史 | 匹配的工具结果、ElicitationResult、Stop 或 SessionEnd |
| Kimi Code | PermissionRequest 工具审批。模型选择和其他 TUI 菜单可用「查看交互」 | PermissionResult、Stop、Interrupt、StopFailure 或 SessionEnd |
| Hermes | human-input hook 的 clarify、approval、sudo。澄清显示题干，sudo 只显示需要到终端输入密码 | human-input resolved 中相同的 request_id |
| Gemini CLI | Notification 的 ToolPermission 提醒 | AfterAgent 或 SessionEnd；工具获准后到回合结束之间可能仍显示提醒 |
| OpenCode | TUI 插件读取当前路由的 permission 和 form 列表，显示每项审批或填表提醒 | 项目从原生列表移除、切换会话，或插件心跳过期 |

从 Codex rollout 推断尚未返回的同步提问时，只有 pane 的 resume 会话 ID 对得上才显示待办。按标题或工作目录找到的记录只进历史。

原生通知按主机、tmux pane ID、pane PID 和 harness 匹配。接收器按会话和请求保存状态。Chat 共享服务器已有的状态轮询。原生事件可以在没有屏幕菜单时显示；操作在 harness 的原生界面完成。OpenCode 插件仅适用于提供下列 TUI 接口的版本。OpenCode 和 Gemini 的完整聊天历史尚无适配器。

已核实的交互默认使用「聊天内显示」。手动「查看交互」默认关闭，可在「设置 → 实验性 → 手动查看交互」开启，开关随账号保存。待答卡片和展开的实时作答区跟在最新消息下方，可直接点选或用键盘作答。开启实验开关后，聊天工具栏会显示位置选择器，可切到「浮窗显示」或选择「仅手动查看」可收起自动卡片，需要时再点「查看交互」。该偏好保存在当前浏览器。自动通知与手动查看共用一张卡片。

Chat 中的「实时终端」默认展开，显示当前 pane 最近 12 行非空输出，保留终端文字的颜色。点标题可收起或展开，收起、暂停或切到后台时停止读取。实验开关开启后，「查看交互」位于终端标题旁，终端片段不可用时，入口回到输入框工具栏。默认的内嵌卡片直接接在输出下方。原生请求和当前画面匹配时显示 ↑、↓ 和 Enter，多题的 Hermes 卡片还可用 ⇤ 和 Tab ⇥ 切题。

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

Hermes 的原生 clarify hook 会记录当前请求 ID 和按顺序排列的题干。主机上的观察器按 pane、进程和 provider 读取未结束的请求。只有单一待答请求的全部题干与当前 Hermes 画面匹配，卡片才启用选项。点击时服务端重新读取 hook 和当前画面，核对请求、题目和选项版本。↑↓ 选择，Enter 回答，←→ 或 Tab 切题。出现另一项审批、请求结束或更换进程后，原卡片的提交会被拒绝。

没有 hook、题干被截断或只有屏幕文字时，「查看交互」可核对原文，「实时作答」可操作原生终端。Codex 有明确会话归属的待答记录、其他 harness 的原生事件均可作为只读通知。旧实验开关和以前保存的应答规则不会触发按键。

Codex 命令审批在「查看交互」中显示待执行命令、仅批准一次、记住前缀（如有）和拒绝。前缀快捷键折到下一行也可识别。收到原生权限请求时卡片自动出现；手动入口开启且仅有屏幕文字时，展开的实时终端识别到完整菜单后，实时终端标题旁的入口会变为「查看命令审批」，可手动查看。更新后的 Codex 观察器会为每次审批回调记录独立标识。只有一个待处理回调，且其命令摘要与屏幕一致时，点「仅批准这一次」或「拒绝」会重新核验当前审批，再发对应快捷键。记住前缀、旧版观察器或只有屏幕预览的选项标有「实时确认」，点击后复核并进入 Chat 内原生审批。实时终端检测到菜单结束时收起对应卡片。

### 模型选择

打开 Chat 模型菜单时才读取目录。Codex 使用该会话 rollout 所在主机和目录对应的 models_cache.json，只展示 visibility 为 list 的模型。缓存是建议列表，账号权限、CLI profile 或服务端策略可能已变化。菜单也能打开该会话的原生 /model 选择器。缺少目录或对端不支持读取时，直接提供原生入口。Claude 的快捷项标为常用别名，Kimi 使用原生菜单，Hermes 展示对应主机的 provider 返回列表。Codex 当前模型来自最近的 turn_context；发出 /model 命令后，等会话记录更新再改变标记。

### 上游接口

- [Codex 原生模型选择器](https://developers.openai.com/codex/cli/slash-commands/) 和 [App Server](https://developers.openai.com/codex/app-server/)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks) 和 [Kimi Code hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html)
- [Hermes 人工输入事件](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) 和 [Gemini CLI hooks](https://geminicli.com/docs/hooks/reference/)
- [OpenCode TUI 的会话、权限与表单接口](https://opencode.ai/docs/build/plugins/cli/)

### Hermes 原生表单

选中 Hermes 会话时，Chat 可以在实时终端上方显示 gateway 当前开放的问题。选好每道题后点「提交全部答案」，也支持多选和「其他」。同一浏览器标签页刷新可恢复未提交的选择。临时草稿按 pane 和请求版本隔离。另一个浏览器或原生终端先答时，Hermes 会关闭同一个请求。普通命令审批提供「仅允许这一次」和「拒绝」。

需要支持共享 gateway 与 terminal breadcrumb 的 Hermes CLI。Linux 主机用当前前台 Hermes 进程、TTY、进程启动时间和该终端的 Hermes 会话记录确认归属。有 hook 时还会核对其 session 和问题。服务只连接正在运行的本机 gateway。终端画面只能提示服务复查。旧版 Hermes、身份不匹配、密码、持久授权和文件编辑审批均可通过实时终端处理。

同一 pane、请求版本与答案重试可取回同一回执。主机端 SQLite 只保存答案摘要和结果，跨进程共享；不同答案无法覆盖先提交的决定。连接中断而结果尚未核实时会提示查看终端。
