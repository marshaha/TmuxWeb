# 记住的授权

agent 问同一件事问十遍（改十个文件、跑十条命令），每次都要点一下。这里把"这道题这么答"
记下来，作用域可选本轮或一直。

## 作用域

| 作用域 | 什么时候失效 |
| --- | --- |
| 本轮（`turn`） | 出现新的用户消息（turn 的边界） |
| 一直（`always`） | 撤销，或 7 天后自动过期 |

## 关键约束（不是装饰，是安全底线）

* **指纹**：只有菜单的标题 + 选项标签 + 开关标签/当前值完全一致，才算"同一道题"
  （`agent_prompts.menu_signature`，sha256 前 16 位）。换一个选项文字就是另一道题。
* **没有指纹不记事**：`POST` 不带 `signature` 一律 422。
* **只有人勾了「记住这个选择」才记**：默认不勾；勾了才会写授权，也才可能自动答。
* **不留痕不进**：自动答一次就 `POST /approvals/{id}/used` 记一笔，卡片上显示"用过 N 次"，
  并写明"已按你的授权自动选了「是」（本轮）"。
* **随时撤销**：卡片上的「撤销」，或 `DELETE /api/panes/{pane}/approvals/{id}`。

## 接口

    GET    /api/panes/{pane}/approvals?turn=…     还在生效的授权
    POST   /api/panes/{pane}/approvals            {signature, action, scope:"turn"|"always", turn, label}
    POST   /api/panes/{pane}/approvals/{id}/used  记一次自动使用
    DELETE /api/panes/{pane}/approvals/{id}       撤销

`GET /prompt` 现在多回两个字段：`signature`（这道题的指纹）和 `grant`（覆盖这道题的授权，
没有就是 `null`）。前端就是靠 `grant` 决定要不要自动答。

## 为什么自动答不算违背"永不自动应答"

原来那条规则防的是"机器替人做主"。现在自动答的前提是**人自己在一次真实点击里勾了记住**，
范围被指纹和作用域锁死，每次都在屏幕上写明它替你点了什么，并且能撤销。
没勾记住的菜单，行为和以前完全一样：只有人点了才发键。

数据落在 `approvals.db`（`.gitignore` 已排除，可用 `TMUX_WEB_APPROVALS_DB` 改路径）。
