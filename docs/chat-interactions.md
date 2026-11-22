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

没有结构化事件的菜单可以在 Chat 查看屏幕原文。Hermes CLI 的完整单选题可点选具名选项，服务器会在发送一个原生数字快捷键前重新检查题目、选项和 pane 身份。自定义答案、多题和未识别的界面可点「实时作答」。此前显式启用「交互菜单卡片」的用户仍可使用实验性的按键和已保存的应答规则。没有安装 hook 时，可点「查看交互」主动读取当前界面。

### 模型选择

打开 Chat 模型菜单时才读取目录。Codex 使用该会话 rollout 所在主机和目录对应的 models_cache.json，只展示 visibility 为 list 的模型。缓存是建议列表，账号权限、CLI profile 或服务端策略可能已变化。菜单也能打开该会话的原生 /model 选择器。缺少目录或对端不支持读取时，直接提供原生入口。Claude 的快捷项标为常用别名，Kimi 使用原生菜单，Hermes 展示对应主机的 provider 返回列表。Codex 当前模型来自最近的 turn_context；发出 /model 命令后，等会话记录更新再改变标记。

### 上游接口

- [Codex 原生模型选择器](https://developers.openai.com/codex/cli/slash-commands/) 和 [App Server](https://developers.openai.com/codex/app-server/)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks) 和 [Kimi Code hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html)
- [Hermes 人工输入事件](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) 和 [Gemini CLI hooks](https://geminicli.com/docs/hooks/reference/)
- [OpenCode TUI 的会话、权限与表单接口](https://opencode.ai/docs/build/plugins/cli/)
