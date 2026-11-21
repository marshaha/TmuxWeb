"""Git helpers for the console: worktrees, checkpoints and undo.

Why a module of its own: the routes need to run a handful of very specific git
incantations and parse their output, and every one of them is worth a unit test
without a real repository on disk.

Vocabulary
    checkpoint  a snapshot ref (`refs/tmuxweb/checkpoints/<stamp>`) that points at
                a commit built by `git stash create` — it captures tracked changes
                without touching the branch, the index or the stash list.
    restore     put the working tree back to a checkpoint (tracked files only;
                files created after the checkpoint are reported, never deleted).

Everything here is pure: build(git args) or parse(text) — no I/O.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

NAMESPACE = "refs/tmuxweb/checkpoints/"
DEFAULT_TIMEOUT = 30


def quote(value: str) -> str:
    return shlex.quote(value)


def build_status(cwd: str) -> str:
    """One call that answers: is this a repo, what branch, what's dirty."""
    return (
        f"cd {quote(cwd)} 2>/dev/null || exit 1; "
        "git rev-parse --show-toplevel 2>/dev/null || exit 2; "
        "echo '--BRANCH--'; git rev-parse --abbrev-ref HEAD 2>/dev/null; "
        "echo '--STATUS--'; git -c core.quotepath=false status --porcelain=v1 2>/dev/null"
    )


def build_list_checkpoints(cwd: str) -> str:
    return (
        f"cd {quote(cwd)} && git for-each-ref --sort=-creatordate "
        f"--format='%(refname)%09%(objectname:short)%09%(creatordate:iso-strict)' "
        f"{quote(NAMESPACE)} 2>/dev/null"
    )


def build_create_checkpoint(cwd: str, label: str) -> str:
    """Snapshot tracked changes into our own ref namespace.

    `git stash create` builds the commit but leaves the stash list, the index and
    the branch alone — exactly what "checkpoint" should mean.
    """
    subject = (label or "checkpoint").replace("\n", " ")[:120]
    return (
        f"cd {quote(cwd)} || exit 1; "
        "root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2; "
        f"cd \"$root\" || exit 2; "
        f"sha=$(git stash create {quote('tmuxweb: ' + subject)} 2>/dev/null); "
        "if [ -z \"$sha\" ]; then echo 'CLEAN'; exit 0; fi; "
        "stamp=$(date +%s); "
        f"ref=\"$(echo {quote(NAMESPACE)})$stamp\"; "
        "git update-ref \"$ref\" \"$sha\" || exit 3; "
        "printf '%s\\t%s\\n' \"$ref\" \"$sha\""
    )


def build_restore(cwd: str, ref: str) -> str:
    """Restore tracked files to `ref`; never delete files that weren't tracked.

    恢复同时动 index 和 worktree（只动 worktree 的话 status 里还留着"改过"的样子），
    最后再比对一次"工作区 == 检查点"，这才是回滚是否成功的唯一标准 ——
    和 HEAD 一致是另一回事，检查点里本来就带着当时未提交的改动。
    """
    return (
        f"cd {quote(cwd)} || exit 1; "
        "root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2; "
        f"cd \"$root\" || exit 2; "
        f"git cat-file -e {quote(ref)}^{{commit}} 2>/dev/null || exit 3; "
        f"(git restore --source={quote(ref)} --staged --worktree -- . 2>/dev/null "
        f" || git checkout {quote(ref)} -- . ) || exit 4; "
        # 回滚不该顺带把东西塞进暂存区：恢复完把索引退回"未暂存"，status 才和平时一样
        f"git reset -q -- . 2>/dev/null; "
        f"git diff --quiet {quote(ref)} -- . 2>/dev/null; echo \"--MATCHES--$?\"; "
        "git -c core.quotepath=false status --porcelain=v1 | head -50"
    )


def build_diff(cwd: str, base: str = "", limit: int = 200_000, path: str = "") -> str:
    inside = f" {quote(base)}" if base else ""
    only = f" -- {quote(path)}" if path else ""
    return (
        f"cd {quote(cwd)} || exit 1; "
        "root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2; "
        f"cd \"$root\" || exit 2; "
        f"echo '--STAT--'; git -c core.quotepath=false diff --stat{inside}{only} 2>/dev/null | tail -20; "
        f"echo '--PATCH--'; git -c core.quotepath=false diff{inside}{only} 2>/dev/null | head -c {int(limit)}"
    )


def build_worktree(cwd: str, name: str, branch: str = "") -> str:
    safe = "".join(ch for ch in (name or "wt") if ch.isalnum() or ch in "-_.") or "wt"
    new_branch = branch or f"wt/{safe}"
    return (
        f"cd {quote(cwd)} || exit 1; "
        "root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2; "
        f"cd \"$root\" || exit 2; "
        f"dest=\"$(dirname \"$root\")/$(basename \"$root\")-wt/{safe}\"; "
        "mkdir -p \"$(dirname \"$dest\")\" || exit 3; "
        f"git worktree add -b {quote(new_branch)} \"$dest\" 2>&1 || exit 4; "
        "printf '%s\\n' \"$dest\""
    )


def build_list_worktrees(cwd: str) -> str:
    return (
        f"cd {quote(cwd)} && git worktree list --porcelain 2>/dev/null | "
        "awk '/^worktree /{p=$2} /^branch /{print p\"\\t\"$2}'"
    )


# ---------------------------------------------------------------- parsing


def parse_status(stdout: str) -> dict:
    """Split the three-section output of build_status()."""
    root, branch, status = "", "", []
    section = "root"
    for line in stdout.splitlines():
        if line == "--BRANCH--":
            section = "branch"
            continue
        if line == "--STATUS--":
            section = "status"
            continue
        if section == "root":
            if line.strip():
                root = line.strip()
        elif section == "branch":
            branch = line.strip()
        else:
            if line.strip():
                status.append(parse_porcelain_line(line))
    return {"root": root, "branch": branch, "changes": status}


def parse_porcelain_line(line: str) -> dict:
    """`XY path` → {code, path, staged, unstaged} (rename shows the source too)."""
    code = line[:2]
    rest = line[3:] if len(line) > 3 else ""
    path = rest
    if " -> " in rest:                     # rename: "old -> new"
        path = rest.split(" -> ", 1)[1]
    return {
        "code": code.strip() or code,
        "path": path.strip(),
        "staged": code[0] not in (" ", "?"),
        "untracked": code.startswith("??"),
    }


def parse_checkpoints(stdout: str) -> list[dict]:
    items = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ref, sha, when = parts[0], parts[1], parts[2]
        items.append({
            "ref": ref,
            "sha": sha,
            "at": when,
            "id": ref.rsplit("/", 1)[-1],
        })
    return items


def parse_worktrees(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            out.append({"path": parts[0], "branch": parts[1].replace("refs/heads/", "")})
    return out


def parse_restore(stdout: str) -> dict:
    """After a restore, what's left in the status is the checkpoint's own diff.

    `matches` says the working tree equals the checkpoint (that is the goal);
    the remaining entries are the changes the checkpoint itself contained, plus
    any untracked file that appeared after it (those are never deleted).
    """
    marker, _, rest = stdout.partition("--MATCHES--")
    matches = marker is not None and rest.split("\n", 1)[0].strip() == "0"
    body = rest.split("\n", 1)[1] if "\n" in rest else ""
    remaining = [parse_porcelain_line(line) for line in body.splitlines() if line.strip()]
    return {
        "matches": matches,
        "remaining": remaining,
        "untracked": [item for item in remaining if item["untracked"]],
    }


@dataclass
class GitError(RuntimeError):
    """Raised when a git command could not be run at all (not when git says no)."""

    detail: str

    def __str__(self) -> str:               # pragma: no cover - trivial
        return self.detail


def build_branch_info(cwd: str) -> str:
    """分支/上游/领先落后/远端 —— 提交前先让人知道会推到哪。"""
    return (
        f"cd {quote(cwd)} || exit 1; "
        "root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2; "
        f"cd \"$root\" || exit 2; "
        "echo '--BRANCH--'; git rev-parse --abbrev-ref HEAD 2>/dev/null; "
        "echo '--UPSTREAM--'; git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null; "
        "echo '--AHEAD--'; git rev-list --count @{u}..HEAD 2>/dev/null || echo 0; "
        "echo '--BEHIND--'; git rev-list --count HEAD..@{u} 2>/dev/null || echo 0; "
        "echo '--REMOTE--'; git remote | head -1; "
        "echo '--GH--'; command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 && echo yes || echo no"
    )


def build_commit_and_push(cwd: str, message: str, push: bool, pr: bool,
                          paths: list[str] | None = None) -> str:
    """暂存 → 提交 →（可选）推送 →（可选）开 PR。

    每一步成功才打 `--STEP--xxx` 标记，失败打 `XXX_FAILED`，这样前端能分清
    "提交成功但推送失败" 和 "根本提交不上"（两者的 sha 和后续动作完全不同）。
    """
    subject = (message or "").strip() or "Update from TmuxWeb"
    target = " ".join(quote(item) for item in (paths or [])) or "-A"
    parts = [
        f"cd {quote(cwd)} || exit 1; ",
        "root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 2; ",
        "cd \"$root\" || exit 2; ",
        f"git add {target} || exit 10; echo '--STEP--stage'; ",
        f"out=$(git commit -m {quote(subject)} 2>&1) || {{ printf '%s\\n' \"$out\" | tail -3; "
        "echo COMMIT_FAILED; exit 11; }; ",
        "printf '%s\\n' \"$out\" | tail -3; echo \"SHA:$(git rev-parse --short HEAD)\"; "
        "echo '--STEP--commit'; ",
    ]
    if push or pr:
        parts.append(
            "out=$(git push -u origin HEAD 2>&1) || { printf '%s\n' \"$out\" | tail -3; "
            "echo PUSH_FAILED; exit 20; }; "
            "printf '%s\n' \"$out\" | tail -3; echo '--STEP--push'; ")
    if pr:
        parts.append(
            "branch=$(git rev-parse --abbrev-ref HEAD); "
            "base=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@'); "
            "[ -n \"$base\" ] || base=main; "
            "if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then "
            "out=$(gh pr create --fill --base \"$base\" --head \"$branch\" 2>&1) || { "
            "printf '%s\n' \"$out\" | tail -3; echo PR_FAILED; exit 30; }; "
            "printf '%s\n' \"$out\" | tail -3; echo '--STEP--pr'; "
            "else echo NO_GH; exit 31; fi; ")
    return "".join(parts)


def parse_branch_info(stdout: str) -> dict:
    sections = {"branch": "", "upstream": "", "ahead": 0, "behind": 0, "remote": "", "gh": False}
    key = ""
    for line in stdout.splitlines():
        if line.startswith("--") and line.endswith("--"):
            key = line.strip("-").lower()
            continue
        value = line.strip()
        if key in ("branch", "upstream", "remote"):
            sections[key] = value
        elif key in ("ahead", "behind"):
            try:
                sections[key] = int(value or 0)
            except ValueError:
                sections[key] = 0
        elif key == "gh":
            sections["gh"] = value == "yes"
    return sections


def suggest_commit_message(changes: list[dict], limit: int = 3) -> str:
    """没有现成标题时，用改动的形状拼一句人话（agent 之后可以再改）。"""
    paths = [item.get("path", "") for item in changes if item.get("path")]
    if not paths:
        return "Update from TmuxWeb"
    if len(paths) == 1:
        return f"Update {paths[0]}"
    head = ", ".join(paths[:limit])
    more = f" 等 {len(paths)} 个文件" if len(paths) > limit else f" 等 {len(paths)} 个文件"
    return f"Update {head}{more}"


def parse_commit_result(stdout: str, exit_code: int) -> dict:
    """看哪几步走到了、PR 链接是多少。"""
    result = {"staged": False, "committed": False, "sha": "", "pushed": False,
              "pr_url": "", "needs_gh": False, "detail": ""}
    step = ""
    for line in stdout.splitlines():
        if line.startswith("--STEP--"):
            step = line.split("--STEP--", 1)[1].strip()
            result[{"stage": "staged", "commit": "committed",
                    "push": "pushed", "pr": "pr_opened"}.get(step, "step")] = True
            continue
        if line.startswith("SHA:"):
            result["sha"] = line.split(":", 1)[1].strip()
        elif line.startswith("https://github.com/") and "/pull/" in line:
            result["pr_url"] = line.strip().split()[0]
        elif "NO_GH" in line:
            result["needs_gh"] = True
        elif line.strip():
            result["detail"] = (result["detail"] + "\n" + line.strip()).strip()
    result.pop("step", None)
    if exit_code:
        result["detail"] = result["detail"] or f"git 返回 {exit_code}"
    return result
