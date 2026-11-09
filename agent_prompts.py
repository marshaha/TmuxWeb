"""把 pane 里的交互菜单读成结构，供聊天界面渲染成按钮。

harness 的 TUI（kimi / claude / codex / hermes）在等待用户选择时会占住整个屏幕，
聊天里只看到"已发送"，人在手机上看不到那个菜单。这个模块只做一件事：从
`tmux capture-pane` 的纯文本里认出三种常见形态，认不出就返回 None —— 绝不猜。

    A 列表     ❯ Trust and continue          （kimi 的 Trust 对话框、模型选择器）
                 Exit
    B 编号     1. Compact and continue        （claude 的权限确认、kimi 的上下文对话框）
               2. ...
    C 确认     [y/N] · Do you want to proceed? · Press Enter to continue

刻意不处理（交给终端模式）：tab 栏、开关（`[ On ] Off`）、进度条、自由文本输入。
这些要理解每个 TUI 的布局语义，猜错等于替用户答错一个不该答的问题。
"""
from __future__ import annotations

import re
from typing import Any

CURSOR_MARKS = ("❯", "➤", "→", "»")
BULLET_MARKS = ("●", "○", "•")

NUMBERED_RE = re.compile(r"^\s{0,8}(\d{1,2})[.)]\s+(\S.*)$")
CURSOR_RE = re.compile(r"^\s{0,8}([❯➤→»]|>\s)\s*(\S.*)$")
BULLET_RE = re.compile(r"^\s{0,8}([●○•])\s+(\S.*)$")
RULE_RE = re.compile(r"^[\s─━═┄┈╭╮╰╯│┃|+*-]{3,}$")
HINT_RE = re.compile(
    r"(↑↓|↑/↓|navigate|Enter (?:select|confirm|to)|Esc (?:cancel|exit|to)|"
    r"shift\+tab|tab toggle|type to search|press enter)", re.I)
MORE_RE = re.compile(r"^\s*[▼▽]\s*(\d+)\s+more\b", re.I)

YES_NO_RE = re.compile(r"(\[y/N\]|\[Y/n\]|\(y/n\)|\(y/N\))", re.I)
PROCEED_RE = re.compile(r"(do you want to (?:proceed|continue|allow)|would you like to|"
                        r"allow this|permission to)", re.I)
PRESS_ENTER_RE = re.compile(r"press enter to (?:continue|confirm)", re.I)

COLUMNS_RE = re.compile(r"\s{2,}")
FOOTER_RE = re.compile(r"(context:\s*\d|ctrl\+o expand|/help|/init:|/yolo)", re.I)


def _clean(line: str) -> str:
    return line.rstrip()


def _split_row(text: str) -> tuple[str, str]:
    """`K3.1 Pro            Kimi Code ↑ current` → ("K3.1 Pro", "Kimi Code ↑ current")"""
    parts = [part.strip() for part in COLUMNS_RE.split(text.strip()) if part.strip()]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _marker(line: str) -> tuple[str, str]:
    """返回 (标记, 后面剩下的文字)。标记是 ❯ / ● / '>' 之类，没有就是 ''。"""
    for pattern in (CURSOR_RE, BULLET_RE):
        match = pattern.match(line)
        if match:
            return match.group(1).strip(), match.group(2)
    return "", line.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


SHELL_RE = re.compile(r"(\S+@\S+:|\$\s+\S|#\s+\S)")


def _looks_like_row(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if SHELL_RE.search(text) or text.endswith("$"):
        return False
    if RULE_RE.match(line) or HINT_RE.search(text) or MORE_RE.match(text) or FOOTER_RE.search(text):
        return False
    if text.startswith(("╭", "╰", "╯", "│", "┃", "─")):
        return False
    return True


def _collect_rows(lines: list[str], anchor: int) -> list[dict]:
    """从光标行向上/向下收同一列的连续选项行（遇到空行、分隔线、提示行就停）。"""
    label_column = _indent(lines[anchor]) + (len(_marker(lines[anchor])[0]) + 1
                                            if _marker(lines[anchor])[0] else 0)
    rows: list[dict] = [dict(line=anchor, cursor=_marker(lines[anchor])[0] in CURSOR_MARKS)]

    def walk(step: int) -> None:
        position = anchor + step
        while 0 <= position < len(lines):
            line = lines[position]
            if not _looks_like_row(line):
                break
            if abs(_indent(line) - label_column) > 2:
                break
            marker, _ = _marker(line)
            rows.append(dict(line=position, cursor=marker in CURSOR_MARKS))
            position += step

    walk(-1)
    walk(1)
    rows.sort(key=lambda item: item["line"])

    options: list[dict] = []
    numbers: list[str] = []
    for row in rows:
        marker, rest = _marker(lines[row["line"]])
        number = ""
        numbered = NUMBERED_RE.match(_clean(lines[row["line"]]))
        if numbered:
            number = numbered.group(1)
            rest = numbered.group(2)
        label, meta = _split_row(rest)
        if not label:
            continue
        numbers.append(number)
        options.append({
            "label": label[:80],
            "meta": meta[:40],
            "selected": marker in CURSOR_MARKS,
            "number": number,
        })
    # 只有整段都带序号才允许发数字（claude 的 ❯ 1. Yes 就是这种）
    numbered_all = bool(options) and all(option["number"] for option in options)
    first_line = min(row["line"] for row in rows)
    return [{"options": options, "numbers": numbered_all, "numbers_seq": numbers,
             "first_line": first_line}]


def _hint_index(lines: list[str], first_line: int) -> int | None:
    """提示行（↑↓ navigate / Esc cancel / type to search）在选项块附近的位置。"""
    # 提示行可能在选项块上方不少（kimi 的信任对话框：标题+提示贴顶部，选项在底部）
    for position in range(max(0, first_line - 14), min(len(lines), first_line + 16)):
        if HINT_RE.search(_clean(lines[position]).strip()):
            return position
    return None


def _title_above(lines: list[str], before: int) -> str:
    """标题一般紧贴提示行的上一行（或选项块上方），不是更早的说明文字。"""
    for position in range(before - 1, max(-1, before - 12), -1):
        text = _clean(lines[position]).strip()
        if not text or MORE_RE.match(text) or RULE_RE.match(text):
            continue
        if _marker(text)[0] or NUMBERED_RE.match(text):
            continue
        return text[:160]
    return ""


def _finish(kind: str, title: str, hint: str, options: list[dict], view: list[str], *,
            numbers: bool, source: str) -> dict[str, Any]:
    more = 0
    for line in view:
        match = MORE_RE.match(_clean(line))
        if match:
            more = int(match.group(1))
    cleaned = []
    for index, option in enumerate(options):
        item = {
            "index": index,
            "label": option.get("label", ""),
            "meta": option.get("meta", ""),
            "selected": bool(option.get("selected")),
        }
        if option.get("key"):
            item["key"] = option["key"]
        if option.get("number"):
            item["number"] = option["number"]
        cleaned.append(item)
    return {
        "kind": kind,
        "title": title,
        "hint": hint,
        "options": cleaned,
        "selected": next((item["index"] for item in cleaned if item["selected"]), 0),
        "numbers": numbers,
        "more": more,
        "source": source,
        "raw": "\n".join(_clean(line) for line in view[-18:]),
    }


def parse_screen(text: str) -> dict[str, Any] | None:
    """认出一个正在等待的交互提示；认不出返回 None。"""
    if not text:
        return None
    lines = text.splitlines()
    # 先丢掉尾部空行：TUI 常在屏幕下方留一大片空白，抓屏连 scrollback 一起给的时候
    # （capture -S -N），不裁掉的话"最后 40 行"全是空行，菜单就被挤出去了
    while lines and not lines[-1].strip():
        lines.pop()
    # 只看最后 40 行：历史里翻过去的旧菜单不能当成"现在还在等"
    lines = lines[max(0, len(lines) - 40):]

    # B：编号列表（优先判，因为它能直接发数字，最稳）
    for offset, raw in enumerate(lines):
        marker, rest = _marker(_clean(raw))
        numbered = NUMBERED_RE.match(rest if marker else _clean(raw))
        if not numbered or numbered.group(1) != "1":
            continue
        block: list[dict] = []
        expected = 1
        position = offset
        while position < len(lines):
            row_marker, row_rest = _marker(_clean(lines[position]))
            match = NUMBERED_RE.match(row_rest if row_marker else _clean(lines[position]))
            if not match or int(match.group(1)) != expected:
                break
            rest = match.group(2)
            label, meta = _split_row(rest)
            block.append({"label": label, "meta": meta,
                          "selected": row_marker in CURSOR_MARKS,
                          "number": match.group(1)})
            expected += 1
            position += 1
        if len(block) >= 2:
            hint_at = _hint_index(lines, offset)
            title = _title_above(lines, hint_at) if hint_at is not None else _title_above(lines, offset)
            hint = _clean(lines[hint_at]).strip()[:160] if hint_at is not None else ""
            return _finish("menu", title, hint, block, lines, numbers=True, source="numbered")

    # A：光标列表
    for offset, raw in enumerate(lines):
        line = _clean(raw)
        if _marker(line)[0] not in CURSOR_MARKS:
            continue
        collected = _collect_rows(lines, offset)[0]
        options = collected["options"]
        if len(options) < 2:
            continue
        top = collected["first_line"]
        hint_at = _hint_index(lines, top)
        # 没有提示行的"列表"多半是 shell 提示符/聊天正文，宁可不出卡片
        if hint_at is None:
            continue
        return _finish("menu", _title_above(lines, hint_at), _clean(lines[hint_at]).strip()[:160],
                       options, lines, numbers=collected["numbers"], source="cursor")

    # C：确认类
    for offset, raw in enumerate(lines):
        line = _clean(raw).strip()
        if YES_NO_RE.search(line):
            return _finish("confirm", line[:160], "",
                           [{"label": "是", "meta": "y + Enter", "key": "yes"},
                            {"label": "否", "meta": "n + Enter", "key": "no"}],
                           lines, numbers=False, source="yesno")
        if PROCEED_RE.search(line):
            options = [{"label": "继续", "meta": "Enter", "key": "enter"},
                       {"label": "取消", "meta": "Esc", "key": "esc"}]
            return _finish("confirm", line[:160], "", options, lines,
                           numbers=False, source="proceed")
        if PRESS_ENTER_RE.search(line):
            return _finish("confirm", line[:160], "",
                           [{"label": "继续", "meta": "Enter", "key": "enter"}],
                           lines, numbers=False, source="enter")
    return None


def choice_keys(prompt: dict[str, Any], index: int) -> list[str]:
    """把"选第 index 项"翻译成按键序列。

    列表没有序号时不能发数字：kimi 的选择器里数字会被当成搜索过滤词，于是列表
    一变，位置就全错了。所以走"先按够多次 ↑ 回到第一项（顶部多余无害），再按
    index 次 ↓，最后 Enter"。
    """
    options = prompt.get("options") or []
    if not options:
        raise ValueError("no options")
    index = max(0, min(int(index), len(options) - 1))
    option = options[index]
    if option.get("key"):
        return {"yes": ["y", "Enter"], "no": ["n", "Enter"],
                "enter": ["Enter"], "esc": ["Escape"]}[option["key"]]
    if prompt.get("numbers") and option.get("number"):
        return [option["number"], "Enter"]
    keys = ["Up"] * (len(options) + 3)
    keys += ["Down"] * index
    keys.append("Enter")
    return keys


def choice_for_key(prompt: dict[str, Any], key: str) -> list[str] | None:
    """Esc/Enter 之类的直接按键；也允许按序号选择。"""
    key = (key or "").lower()
    if key in ("esc", "escape", "cancel"):
        return ["Escape"]
    if key in ("enter", "confirm", "ok"):
        return ["Enter"]
    if key in ("yes", "y"):
        return ["y", "Enter"]
    if key in ("no", "n"):
        return ["n", "Enter"]
    if key.isdigit():
        return choice_keys(prompt, int(key) - 1)
    return None
