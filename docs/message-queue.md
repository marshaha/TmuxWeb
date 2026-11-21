# 持久排队

agent 正忙的时候，消息不该硬塞进去（粘贴+回车会被那一轮的输出吞掉），也不该只存在浏览器里
（重启服务就没了）。这里是一个按窗格排队的持久队列：**存进 SQLite，等这一轮跑完自动发出去**。

## 三种情况

| 场景 | 行为 |
| --- | --- |
| 窗格空闲 | 立即发送（和以前一样） |
| 窗格在跑（`state.waiting`） | 前端自动排到服务端队列，输入框上方出现一条胶囊，可以 × 取消 |
| 想让这一轮立刻停 | 点胶囊上的 ⚡，把这条改成 `steer`：worker 先按中断键（Escape），再发文字 |

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/panes/{pane}/queue` | 这个窗格的待发消息（`pending` / `sending` / `failed`） |
| POST | `/api/panes/{pane}/queue` | `{"text","submit","mode":"queue"\|"steer"}` |
| POST | `/api/panes/{pane}/queue/{id}/mode` | 改投递方式（还没发出去的才行） |
| DELETE | `/api/panes/{pane}/queue/{id}` | 取消 |

## worker

* 每 2 秒扫一遍有 `pending` 的窗格；
* 判断"忙不忙"用的是会话文件（`agent_status`），**忙就跳过，不消耗重试次数**；
* 领到一条就标 `sending`，发成功记 `sent`，失败记 `retry_later`；
* 尝试次数上限 3 次，用完标 `failed`（胶囊会变红，可以取消）；
* 进程重启时，`sending` 的行会被放回 `pending` —— 那次发送没有确认过，必须重来。

数据落在 `message_queue.db`（`.gitignore` 里已排除，可用 `TMUX_WEB_QUEUE_DB` 改路径）。

## 已知边界

* 远端（SSH）窗格没有本地会话文件，"忙不忙"判断不出结果，会当成空闲直接发 ——
  要精确判断得等远端也上报状态。
* 附件消息不排队（附件已经上传到 `upload_files`，排队会让语义变复杂）。
