# 检查点、回滚和 worktree

agent 把代码改乱了以后，最需要的是"退回到一分钟前"。这三个接口就是干这个的，
全部走 `run_exec`（本地 `bash -lc`，SSH 主机走同一个 runner），联邦子节点没有 shell，
会明确回 501。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/panes/{pane}/git` | 是不是仓库、分支、脏文件、worktree 列表、检查点列表 |
| POST | `/api/panes/{pane}/git/checkpoint` | `{"label":"..."}` 把当前改动快照成 ref |
| POST | `/api/panes/{pane}/git/restore` | `{"ref":"refs/tmuxweb/checkpoints/…"}` 回到那个快照 |
| GET | `/api/panes/{pane}/git/diff?base=<ref>` | `--stat` + 补丁（默认 200 KB 上限） |
| POST | `/api/panes/{pane}/git/worktree` | `{"name":"task1"}` 开一份独立 worktree + 分支 |

## 检查点是怎么做的

    git stash create "tmuxweb: <label>"   # 造一个 commit，但不动分支/index/stash 列表
    git update-ref refs/tmuxweb/checkpoints/<epoch> <sha>

所以检查点是**引用**，不是提交：`git log` 看不到，分支不受影响，`git branch -a` 也不会变。
删掉它用 `git update-ref -d <ref>`。

工作区干净时 `git stash create` 什么都不输出，接口回 `{"created": false}`，不会造假检查点。

## 回滚的语义

    git restore --source=<ref> --staged --worktree -- .

判定标准是"工作区 == 检查点"（响应里的 `matches`），不是"工作区 == HEAD"——
检查点里本来就带着当时未提交的改动，所以回滚后 `git status` 仍然会显示那些改动，这是对的。

**检查点之后新增的未跟踪文件不会被删除**，它们会在响应的 `untracked` 里列出来，
要不要清理由人决定。

## worktree

    git worktree add -b wt/<name> ../<repo>-wt/<name>

放在仓库旁边的 `<repo>-wt/` 目录，分支名 `wt/<name>`。多个任务并行时不互相踩文件；
注意 worktree 只隔离文件，不隔离端口和数据库。
