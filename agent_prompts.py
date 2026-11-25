"""把 pane 里的交互菜单读成结构，供聊天界面渲染成按钮。

harness 的 TUI（kimi / claude / codex / hermes）在等待用户选择时会占住整个屏幕，
聊天里只看到"已发送"，人在手机上看不到那个菜单。这个模块只做一件事：从
`tmux capture-pane` 的纯文本里认出几种常见形态，认不出就返回 None —— 绝不猜。

    A 列表     ❯ Trust and continue          （kimi 的 Trust 对话框、模型选择器）
                 Exit
    B 编号     1. Compact and continue        （claude 的权限确认、kimi 的上下文对话框）
               2. ...
    C 确认     [y/N] · Do you want to proceed? · Press Enter to continue

还支持带按键说明的分段开关和多选框。Tab 栏、进度条、自由文本输入交给终端。
这些要理解每个 TUI 的布局语义，猜错等于替用户答错一个不该答的问题。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CURSOR_MARKS = ("❯", "➤", "→", "»", "›", "▸", "▶")
BULLET_MARKS = ("●", "○", "•")

NUMBERED_RE = re.compile(r"^\s{0,8}(\d{1,2})[.)]\s+(\S.*)$")
# 菜单项都很短，而且不会以句号/叹号结尾；这条主要用来挡住"聊天正文里的一段编号"
OPTION_MAX = 160   # codex 的更新项里带一条完整命令，比一般菜单项长得多
DESCRIPTION_MAX = 160   # 选项描述行（label 下一行缩进的说明文字）和 label 同长
TOGGLE_SEGMENT_MAX = 14
SENTENCE_END = ("。", "．", ".", "！", "!", "？")
CURSOR_RE = re.compile(r"^\s{0,8}([❯➤→»›▸▶]|>\s)\s*(\S.*)$")
BULLET_RE = re.compile(r"^\s{0,8}([●○•])\s+(\S.*)$")
RULE_RE = re.compile(r"^[\s─━═┄┈╭╮╰╯│┃|+*-]{3,}$")
HINT_RE = re.compile(
    r"(↑↓|↑/↓|←→|←/→|navigate|Enter (?:select|confirm|to)|Esc (?:cancel|exit|to)|"
    r"shift\+tab|tab toggle|type to search|press enter|arrow keys to (?:move|navigate)|"
    r"方向键(?:导航|选择)|回车(?:确认|提交|选择)|Esc 取消)", re.I)
BRACKET_RE = re.compile(r"\[\s*([^\]]{1,24}?)\s*\]")
BOX_ROW_RE = re.compile(r"^\s*[│┃║] ?(.*?)[│┃║]\s*$")
CHECKBOX_RE = re.compile(r"^\s{0,8}(?:(?P<cursor>[❯➤→»›▸▶>])\s*)?"
                         r"(?P<box>\[[ xX✓]\]|[☐☑✓])\s+(?P<label>\S.*)$")
SPACE_TOGGLE_RE = re.compile(
    r"(?:space(?:bar)?\s+(?:(?:to|:)\s*)?(?:toggle|select|mark)|"
    r"(?:toggle|select)\s+(?:with|using)\s+space|空格(?:键)?(?:选择|切换))", re.I)
ENTER_SUBMIT_RE = re.compile(
    r"(?:enter\s+(?:(?:to|:)\s*)?(?:submit|confirm|continue|accept|done)|"
    r"(?:submit|confirm)\s+(?:with|using)\s+enter|回车(?:确认|提交))", re.I)
MOVE_HINT_RE = re.compile(r"(?:[↑↓].{0,4}[↑↓]|(?:arrow keys|arrows)\s+(?:to\s+)?(?:move|navigate))", re.I)
MORE_RE = re.compile(r"^\s*[▼▽]\s*(\d+)\s+more\b", re.I)

YES_NO_RE = re.compile(r"(\[y/N\]|\[Y/n\]|\(y/n\)|\(y/N\))", re.I)
PROCEED_RE = re.compile(r"(do you want to (?:proceed|continue|allow)|would you like to|"
                        r"allow this|permission to)", re.I)
PRESS_ENTER_RE = re.compile(r"press enter to (?:continue|confirm)", re.I)

COLUMNS_RE = re.compile(r"\s{2,}")
FOOTER_RE = re.compile(r"(context:\s*\d|ctrl\+o expand|/help|/init:|/yolo|"
                       r"for shortcuts|^\s*\?\s|release notes|https?://)", re.I)


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


# 被 echo 出来的命令/源码里也会出现 "press Enter to continue"。
# 真的对话框只有一句话；带这些痕迹的一律不是对话框。
CODE_MARKERS = ("sys.stdout", "print(", "console.log", "\\r\\n", "\\n", '"""', ");")
SHELL_PROMPT_RE = re.compile(r"^\s*(\$|>|>>>|#)\s")
CONFIRM_MAX = 120


def _looks_like_code(line: str) -> bool:
    """这一行像被回显的源码/命令，而不是对话框在问你。"""
    text = line.strip()
    if len(text) > CONFIRM_MAX:
        return True
    if SHELL_PROMPT_RE.match(text):
        return True
    if any(marker in text for marker in CODE_MARKERS):
        return True
    # 落单的引号、或"注释 + 引号"（`# ... "press enter to continue"`）都是源码特征；
    # 真对话框要引用文件名时引号是成对的（`Overwrite "a.txt"? [y/N]`）
    if text.count('"') % 2:
        return True
    if "# " in text and '"' in text:
        return True
    # `foo("...")` 这种以调用收尾的
    return bool(re.search(r"[\w\]\)]\([^)]*\)\s*$", text))


def _option_is_plausible(label: str) -> bool:
    """菜单项长得像菜单项吗：短、不以句号结尾。"""
    text = label.strip()
    if not text or len(text) > OPTION_MAX:
        return False
    return not text.endswith(SENTENCE_END)


def _looks_like_row(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if SHELL_RE.search(text) or text.endswith("$"):
        return False
    if RULE_RE.match(line) or HINT_RE.search(text) or MORE_RE.match(text) or FOOTER_RE.search(text):
        return False
    if BRACKET_RE.search(text):
        return False
    if text.startswith(("╭", "╰", "╯", "│", "┃", "─")):
        return False
    return True


def _looks_like_description(line: str, threshold_column: int) -> bool:
    """这一行像不像某选项的描述行：比 threshold 列更靠右、无标记、够短、
    不是提示/页脚/分隔/开关行。walk 里跳过描述行与 _description_after
    认描述行用同一条判定，别各写一套。

    threshold 的取法分两种，原因不同：
    - 光标列表：选项行本身就占 label_column..label_column+1 两列（无标记行
      要给 ❯ 留位，见 MODEL_PICKER），所以 threshold 取 label_column+1，
      描述行必须比任何选项行都深；
    - 编号列表：同级行都带序号、不会被误认，所以 threshold 取选项行自身的
      缩进，描述行只要比行首更靠右就算。
    """
    if not _looks_like_row(line):
        return False
    if _indent(line) <= threshold_column:
        return False
    marker, _ = _marker(line)
    if marker:
        return False
    if NUMBERED_RE.match(_clean(line)):
        return False
    text = _clean(line).strip()
    return bool(text) and len(text) <= DESCRIPTION_MAX


def _description_after(lines: list[str], row_line: int, threshold_column: int) -> str:
    """选项行正下方那一行是不是它的描述。

    Claude 的 AskUserQuestion 式菜单把描述写在 label 下一行、缩进更深：
        ❯ Quick fix
            Patch the bug in place
    label 换行续写一般和 label 同列，所以不会被误认成描述；认不出来
    就返回 ""，不猜。描述行缩进不够深（和选项行同列）时宁可漏认——
    卡片少一行描述只是少信息，把选项行吃掉却是答错题。
    """
    nxt = row_line + 1
    if nxt >= len(lines):
        return ""
    if not _looks_like_description(lines[nxt], threshold_column):
        return ""
    return _clean(lines[nxt]).strip()


def _collect_rows(lines: list[str], anchor: int) -> list[dict]:
    """从光标行向上/向下收同一列的连续选项行（遇到空行、分隔线、提示行就停）。"""
    # 标签实际起始列：光标标记后面可能不止一个空格（`❯    选项 A`），
    # 按"标记长度 + 1"算会把后续几行判成不同列，整块只剩第一项
    anchor_text = _clean(lines[anchor])
    anchor_marker, anchor_rest = _marker(anchor_text)
    if anchor_marker and anchor_rest:
        # _clean 只去尾部空白，行首空格还在，find 的结果本身就是绝对列
        label_column = anchor_text.find(anchor_rest)
    else:
        label_column = _indent(lines[anchor])
    rows: list[dict] = [dict(line=anchor, cursor=_marker(lines[anchor])[0] in CURSOR_MARKS)]

    def walk(step: int) -> None:
        position = anchor + step
        while 0 <= position < len(lines):
            line = lines[position]
            if not _looks_like_row(line):
                break
            # 描述行不是选项：跳过它继续走，不能把它收成一项、也不能截断菜单
            if _looks_like_description(line, label_column + 1):
                position += step
                continue
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
        item = {
            "label": label[:80],
            "meta": meta[:40],
            "selected": marker in CURSOR_MARKS,
            "number": number,
        }
        description = _description_after(lines, row["line"], label_column + 1)
        if description:
            item["description"] = description
        options.append(item)
    # 只有整段都带序号才允许发数字（claude 的 ❯ 1. Yes 就是这种）
    numbered_all = bool(options) and all(option["number"] for option in options)
    return [{"options": options, "numbers": numbered_all, "numbers_seq": numbers,
             "first_line": min(row["line"] for row in rows),
             "last_line": max(row["line"] for row in rows)}]


def _hint_index(lines: list[str], first_line: int, last_line: int | None = None,
                above: int = 6, below: int = 6) -> int | None:
    """提示行（↑↓ navigate / Esc cancel / type to search）在选项块附近的位置。

    没有提示行的"列表"就不是菜单 —— 聊天记录里用户自己打的 `1. 2. 3.` 曾经被当成
    菜单，点一下就往 pane 里敲了一个 "1"。所以这个窗口必须贴着选项块。
    """
    tail = first_line if last_line is None else max(first_line, last_line)
    low, high = max(0, first_line - above), min(len(lines), tail + below)
    hits = [position for position in range(low, high)
            if HINT_RE.search(_clean(lines[position]).strip())]
    if not hits:
        return None
    return min(hits, key=lambda position: abs(position - first_line))


def _title_above(lines: list[str], before: int) -> str:
    """标题一般紧贴提示行的上一行（或选项块上方），不是更早的说明文字。

    codex 的信任对话框把问题折成两三行，标题行本身不以问号结尾，所以先往上找
    最近的一行「以问号结尾」，那才是真正的问题。
    """
    for position in range(before - 1, max(-1, before - 9), -1):     # 问题常被折成几行，往上找 8 行
        text = _clean(lines[position]).strip()
        if (len(text) > 12 and ("?" in text or "？" in text)
                and not HINT_RE.search(text) and not FOOTER_RE.search(text)):
            return text[:160]
    for position in range(before - 1, max(-1, before - 12), -1):
        text = _clean(lines[position]).strip()
        if not text or MORE_RE.match(text) or RULE_RE.match(text):
            continue
        if FOOTER_RE.search(text):
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
        if option.get("description"):
            # 描述行只做展示：不进签名（needs_input.prompt_signature），
            # 描述换行重排不该让稳定计数重来
            item["description"] = option["description"][:DESCRIPTION_MAX]
        if index == 0:
            # 首选项标 Recommended：对标 Open WebUI ask_user（提问者把推荐项
            # 列在第一位，UI 给首选项打 Recommended）。这只是位置标记——
            # 卡片从不替用户作答，标记不改变任何判定。
            item["recommended"] = True
        if option.get("key"):
            item["key"] = option["key"]
        if option.get("number"):
            item["number"] = option["number"]
        if "checked" in option:
            item["checked"] = option["checked"]
            item.pop("recommended", None)
        cleaned.append(item)
    prompt = {
        "kind": kind,
        "toggles": [] if kind == "multi" else _toggle_rows(view),
        "title": title,
        "hint": hint,
        "options": cleaned,
        "selected": next((item["index"] for item in cleaned if item["selected"]), 0),
        "cursor": any(item["selected"] for item in cleaned),
        "numbers": numbers,
        "more": more,
        "source": source,
        "raw": "\n".join(_clean(line) for line in view[-18:]),
    }
    prompt["revision"] = action_revision(prompt)
    return prompt


def action_revision(prompt: dict) -> str:
    """Bind a click to the labels, controls and cursor actually displayed."""
    options = prompt.get("options") or []
    toggles = prompt.get("toggles") or []
    data = [prompt.get(key) for key in ("kind", "source", "title", "hint",
                                       "selected", "cursor", "numbers", "more")]
    data += [[tuple(item.get(key) for key in ("label", "description", "meta",
                                             "number", "key", "checked"))
              for item in options],
             [tuple(item.get(key) for key in ("label", "options", "active", "selected"))
              for item in toggles]]
    return hashlib.sha256(json.dumps(data, ensure_ascii=True).encode()).hexdigest()[:32]


def _multiselect(lines: list[str]) -> dict | None:
    """Recognize an explicitly documented arrow/Space/Enter checklist."""
    for footer in range(len(lines) - 1, max(-1, len(lines) - 7), -1):
        if CHECKBOX_RE.match(lines[footer]):
            continue
        # Related controls can be printed on adjacent footer lines.
        hint = " ".join(line.strip() for line in lines[footer:footer + 3])
        if not all(pattern.search(hint) for pattern in
                   (SPACE_TOGGLE_RE, ENTER_SUBMIT_RE, MOVE_HINT_RE)):
            continue
        end = footer
        while end > 0 and (not lines[end - 1].strip() or RULE_RE.match(lines[end - 1])):
            end -= 1
        start = end
        while start > 0 and CHECKBOX_RE.match(lines[start - 1]):
            start -= 1
        options = []
        for line in lines[start:end]:
            row = CHECKBOX_RE.match(line)
            label = row["label"].strip()
            if len(label) > OPTION_MAX:
                break
            options.append({"label": label, "selected": bool(row["cursor"]),
                            "checked": row["box"] not in {"[ ]", "☐"}})
        if (len(options) >= 2 and len(options) == end - start
                and sum(option["selected"] for option in options) == 1
                and not any(MORE_RE.match(line) for line in lines[end:footer + 3])):
            return _finish("multi", _title_above(lines, start), hint[:160],
                           options, lines, numbers=False, source="checkbox")
    return None


def _toggle_rows(lines: list[str]) -> list[dict]:
    """认 `[ High ]` 这种开关行：一行若干段，正好一段带方括号就是当前值。

        Low    [ High ]    Max          ← kimi 的 Thinking
        [ On ]    Off
    """
    found: list[dict] = []
    # 同一个开关会随重绘在历史里出现多次，每次的当前值还可能不同 ——
    # 按"分段内容"去重并保留最后（最新）那一次，否则卡片里会出现好几个同名开关
    seen: dict[tuple, dict] = {}
    for offset, raw in enumerate(lines):
        line = _clean(raw)
        brackets = BRACKET_RE.findall(line)
        if len(brackets) != 1:
            continue
        segments = [part for part in COLUMNS_RE.split(line.strip()) if part.strip()]
        if len(segments) < 2:
            continue
        options, active = [], -1
        for index, segment in enumerate(segments):
            inner = BRACKET_RE.fullmatch(segment.strip())
            if inner:
                options.append(inner.group(1).strip())
                active = index
            elif segment.strip().startswith("[") or segment.strip().endswith("]"):
                options = []          # 半截方括号：不是开关，别猜
                break
            else:
                options.append(segment.strip())
        if active < 0 or len(options) < 2 or len(options) > 5:
            continue
        if not all(len(option) <= TOGGLE_SEGMENT_MAX for option in options):
            continue
        if any(token in line for token in ("，", "。", "：")) and len(options) > 4:
            continue
        label, hint = "", ""
        for probe in range(offset - 1, max(-1, offset - 4), -1):
            text = _clean(lines[probe]).strip()
            if not text or RULE_RE.match(text) or MORE_RE.match(text):
                continue
            if BRACKET_RE.search(text) or FOOTER_RE.search(text):
                continue
            # `Thinking  (←→ to switch)` 这种：括号前是名字、括号里是提示；
            # 整行就是提示（`↑↓ navigate · Enter select`）的不算名字
            head = re.split(r"[（(]", text)[0].strip()
            hint_match = re.search(r"([↑↓←→/ ]{1,6}\s*to (?:switch|toggle)[^)]*)", text)
            if HINT_RE.search(head) and not hint_match:
                continue
            label = re.split(r"\s{2,}", head)[0].strip()
            hint = hint_match.group(1).strip() if hint_match else ""
            break
        if not hint:
            # 提示也可能单独占一行（"←→ to switch · Enter confirm"），开关行附近找一下
            for probe in range(max(0, offset - 4), min(len(lines), offset + 4)):
                text = _clean(lines[probe]).strip()
                found_hint = re.search(r"((?:[←→↑↓/ ]|left|right|arrow){1,20}to (?:switch|toggle)[^)]*)",
                                       text, re.I)
                if found_hint:
                    hint = found_hint.group(1).strip()
                    break
        entry = {"index": len(found), "label": label[:40], "hint": hint[:60],
                 "options": [option[:24] for option in options], "active": active}
        signature = tuple(entry["options"])
        if signature in seen:
            known = seen[signature]
            known["active"] = active                       # 最新的值才算数
            known["label"] = known["label"] or entry["label"]
            known["hint"] = known["hint"] or entry["hint"]
            continue
        seen[signature] = entry
        found.append(entry)
    return found


def toggle_keys(prompt: dict[str, Any], toggle_index: int, value_index: int) -> list[str]:
    """把"要第几个值"翻译成 ←/→ 次数（相对当前值，不假设焦点在哪）。"""
    toggles = prompt.get("toggles") or []
    if not toggles:
        raise ValueError("no toggle")
    if not 0 <= toggle_index < len(toggles):
        raise ValueError("toggle index is out of range")
    toggle = toggles[toggle_index]
    if not 0 <= value_index < len(toggle["options"]):
        raise ValueError("toggle value is out of range")
    target = value_index
    delta = target - toggle["active"]
    if delta == 0:
        return []
    return ["Right"] * delta if delta > 0 else ["Left"] * (-delta)


_CURSOR_HINT = re.compile(r"(?:Enter\s+(?:to\s+)?(?:select|confirm|submit|accept)|回[车車](?:确认|確認|提交|选择|選擇))", re.I)
_EDITOR_TOP = re.compile(r"(?:╭─{3,}╮|┌─{3,}┐|─{4,}.*─{4,})")
_EDITOR_BOTTOM = re.compile(r"(?:╰─{3,}╯|└─{3,}┘|─{4,})")
_SGR = re.compile(r"\x1b\[[0-9;:]*m")


def possible_cursor_menu(text: str) -> bool:
    """Cheap gate on the already captured tail; ordinary output needs no extra tmux call."""
    return bool(text and "─" in text and _CURSOR_HINT.search(text) and MOVE_HINT_RE.search(text))


def _cursor_menu_labels(options, physical, y):
    """Require one selected row and a consistent layout for every option."""
    labels, selected = [], []
    numbered = None
    label_column = None
    for index, (i, text) in enumerate(options, 1):
        marker = re.match(r"^[›❯▸▶]\s+", text)
        label = text[marker.end():] if marker else text
        number = NUMBERED_RE.fullmatch(label)
        if numbered is None:
            numbered = bool(number)
        if bool(number) != numbered:
            return None
        if number:
            if int(number[1]) != index:
                return None
            label = number[2]
        else:
            row = _SGR.sub("", physical[i])
            column = len(row) - len(row.lstrip(" ")) + (marker.end() if marker else 0)
            if label_column is not None and column != label_column:
                return None
            label_column = column
        if not label or len(label) > OPTION_MAX:
            return None
        labels.append(label)
        # A standalone inverse-video SGR before the label is unambiguous.
        # An indexed foreground/background colour numbered 7 is unrelated.
        if marker or re.match(r"^\s*\x1b\[7m\s*\S", physical[i]):
            selected.append(i)
    return labels if selected == [y] else None


def cursor_notice_from_evidence(menu) -> dict | None:
    """Rebuild a display-only notice, including when the evidence came from a child."""
    if not isinstance(menu, dict):
        return None
    title, choices, selected = menu.get("question"), menu.get("choices"), menu.get("selected")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 20:
        return None
    if type(selected) is not int or not 0 <= selected < len(choices):
        return None
    if any(not isinstance(text, str) or not text.strip() or len(text) > OPTION_MAX
           or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text) for text in (title, *choices)):
        return None
    signature = hashlib.sha256(json.dumps([title, choices]).encode()).hexdigest()[:32]
    return {"kind": "notice", "source": "cursor_menu_notice", "read_only": True,
            "title": "终端可能正在等你选择", "hint": title, "options": [],
            "choices": choices, "selected": selected, "revision": "cursor-" + signature}


def cursor_menu_notice(frame: str, pane_id: str, pid: str) -> dict | None:
    rows = frame.split("\n")
    if rows and not rows[-1]:
        rows.pop()
    if len(rows) < 5 or rows[0] != rows[-1]:
        return None
    meta = rows[0].split()
    if (len(meta) != 8 or meta[:2] != [pane_id, str(pid)] or meta[6:] != ["1", "0"]
            or not all(v.isascii() and v.isdecimal() and len(v) <= 6 for v in meta[2:6])):
        return None
    width, height, x, y = map(int, meta[2:6])
    physical = rows[1:-1]
    if len(physical) != height or not (0 <= x < width and 0 <= y < height):
        return None
    plain = [_SGR.sub("", row).rstrip() for row in physical]
    # Only a bounded editor with its prompt glyph establishes an input region.
    editors = []
    for top, row in enumerate(plain[:-2]):
        if not _EDITOR_TOP.fullmatch(row.strip()):
            continue
        boxed = row.strip()[0] in "╭┌"
        first = plain[top + 1].strip()
        if boxed:
            if not (first.startswith("│") and first.endswith("│")):
                continue
            first = first[1:-1].strip()
        if not re.match(r"^[>❯›](?:\s|$)", first):
            continue
        for bottom in range(top + 2, min(top + 16, height)):
            border = plain[bottom].strip()
            if _EDITOR_BOTTOM.fullmatch(border) and ((border[0] in "╰└") == boxed):
                editors.append((top, bottom))
                break
            if boxed and not (border.startswith("│") and border.endswith("│")):
                break
    if len(editors) != 1:
        return None
    top, bottom = editors[0]
    # Constrain the cursor to the visible option text. Restricting the right
    # edge to code-point length is conservative for wide terminal glyphs.
    row = plain[y]
    if not (len(row) - len(row.lstrip(" ")) <= x < len(row)) or top <= y <= bottom:
        return None
    # Only the block directly adjacent to the editor is eligible. Ignore older output.
    start, end = (max(0, top - 20), top) if y < top else (bottom + 1, min(height, bottom + 21))
    if not start <= y < end:
        return None
    block = [(i, plain[i].strip()) for i in range(start, end) if plain[i].strip()]
    for hint_idx, (_, hint) in enumerate(block):
        if not (MOVE_HINT_RE.search(hint) and _CURSOR_HINT.search(hint)):
            continue
        if hint_idx != len(block) - 1:
            continue
        for title_idx in range(hint_idx - 3, -1, -1):
            _, title = block[title_idx]
            if len(title) > 160 or not (title.endswith(("?", "？")) or re.match(r"^(?:Select|Choose)\s", title, re.I)):
                continue
            options = block[title_idx + 1:hint_idx]
            labels = _cursor_menu_labels(options, physical, y)
            if not labels:
                continue
            selected = next(index for index, (row, _) in enumerate(options) if row == y)
            return cursor_notice_from_evidence({"question": title, "choices": labels, "selected": selected})
    return None


def codex_command_approval(text: str) -> dict | None:
    """Describe a complete command approval at the bottom of the Codex TUI."""
    rows = (text or "").rstrip().splitlines()[-48:]
    if not rows or rows[-1].strip().lower() != "press enter to confirm or esc to cancel":
        return None
    start = next((i for i in range(len(rows) - 2, -1, -1)
                  if rows[i].lstrip().startswith("$ ")), None)
    if start is None:
        return None
    command = [rows[start].strip()[2:]]
    options = []
    cursors = []
    for row in rows[start + 1:-1]:
        if not row.strip():
            continue
        option = re.fullmatch(r"\s*(?:(›)\s+)?([1-3])\.\s+(.+)", row)
        if option:
            if int(option[2]) != len(options) + 1:
                return None
            if option[1]:
                cursors.append(len(options))
            options.append(option[3].strip())
        elif row.startswith("  "):
            if options:
                options[-1] += " " + row.strip()
            else:
                command.append(row.strip())
        else:
            return None
    if len(cursors) != 1 or len(options) not in (2, 3):
        return None
    if options[0] != "Yes, proceed (y)" or options[-1] != "No, and tell Codex what to do differently (esc)":
        return None
    labels = [{"label": "仅批准这一次"}, {"label": "拒绝，返回 Codex"}]
    if len(options) == 3:
        rule = re.fullmatch(r"Yes, and don't ask again for commands that start with `([^`]+)` \(p\)", options[1])
        if not rule:
            return None
        labels.insert(1, {"label": "批准并记住命令前缀", "description": rule[1]})
    cmd = " ".join(command)
    if not cmd.strip():
        return None
    labels[0]["description"] = "$ " + cmd
    return {"kind": "notice", "source": "codex_command_approval", "title": "Codex 命令审批",
            "hint": "选好后点「确认选择」打开实时审批，在 Codex 中确认。", "options": labels,
            "selected": cursors[0], "read_only": True,
            "revision": hashlib.sha256(json.dumps([command, options]).encode()).hexdigest()}


def hermes_question(text: str, pane: dict, *, expected_questions: str | None = None) -> dict | None:
    """One complete Hermes clarify dialog. Digits lock a choice without Enter."""
    if not text or "↑/↓" not in text or pane.get("provider") != "hermes":
        return None
    lines = [line.strip() for line in text.splitlines()[-45:] if line.strip()]
    # The live clarify composer is below the box. Historical output and a
    # transcript quoting the dialog must not acquire answer buttons.
    if len(lines) < 8 or not any(re.fullmatch(r"\?\s*❯", line) for line in lines[-4:]):
        return None
    headers = [i for i, line in enumerate(lines) if line.startswith("╭") and
               any(s in line for s in (" needs your input", "需要你的回答", "需要你的回覆"))]
    if not headers:
        return None
    start = headers[-1] + 1
    rows = []
    for line in lines[start:]:
        row = BOX_ROW_RE.fullmatch(line)
        if not row:
            break
        rows.append(row[1].strip())
    end = start + len(rows)
    if (len(rows) < 4 or len(lines) <= end + 2 or not lines[end].startswith("╰")
            or not re.fullmatch(r"↑/↓ (?:to select, Enter to lock, Tab next question|选择，Enter 锁定，Tab 下一个问题|選擇，Enter 鎖定，Tab 下一題)(?:\s+\(\d+s\))?", lines[end + 1])
            or not any(re.fullmatch(r"\?\s*❯", line) for line in lines[end + 2:end + 5])):
        return None
    count = re.fullmatch(r"([1-9]\d?) (?:questions?|个问题|個問題)", rows[0])
    headings = [i for i, row in enumerate(rows[1:], 1) if row.startswith(("▸ ", "· ", "✓ "))]
    if not count or len(headings) != int(count[1]) or headings[0] != 1:
        return None
    # Hermes expands one group at a time. A revisited answered question has
    # a check mark too, so the numbered block identifies the active group.
    groups = [rows[start:stop] for start, stop in zip(headings, headings[1:] + [len(rows)])]
    if expected_questions is not None and "\n".join(group[0][2:] for group in groups) != expected_questions:
        return None
    expanded = [i for i, group in enumerate(groups)
                if any(re.fullmatch(r"(?:❯\s+)?1\.\s+.+", row) for row in group[1:])]
    if len(expanded) != 1:
        return None
    active = expanded[0]
    group = groups[active]
    if group[0].startswith("· ") or any(g[0].startswith("▸ ") for i, g in enumerate(groups) if i != active):
        return None
    options = []
    first = next(i for i, row in enumerate(group[1:], 1) if re.fullmatch(r"(?:❯\s+)?1\.\s+.+", row))
    if first != 1 and not group[0].startswith("✓ "):
        return None
    for i, row in enumerate(group[first:], 1):
        m = re.fullmatch(r"(❯\s+)?([0-9])\.\s+(.+)", row)
        if not m or i > 10 or m[2] != str(i % 10):
            return None
        options.append({"label": m[3], "number": m[2], "selected": bool(m[1])})
    if (len(options) < 2 or sum(o["selected"] for o in options) != 1 or options[-1]["label"] not in
            ("Other (type your answer)", "其他（输入你的回答）", "其他（輸入你的答案）")):
        return None
    # Hermes switches to its text composer when the last shortcut is pressed.
    prompt = _finish("menu", group[0][2:], "选好后点「确认选择」。自定义内容可在实时终端填写。",
                     options, [], numbers=True, source="hermes_clarify")
    if len(groups) > 1:
        prompt["question_count"] = len(groups)
        prompt["hint"] = f"第 {active + 1}/{len(groups)} 题 · ↑↓ 选项 · 确认选择后作答 · ←→ 切题"
    prompt["options"][-1]["opens_input"] = True
    for option in prompt["options"]:
        option.pop("recommended", None)
        option["keys"] = [option["number"]]
    identity = [pane.get("pane_id") or pane.get("id"), pane.get("pane_pid"), prompt["revision"]]
    if len(groups) > 1:
        identity.extend((active, rows))
    prompt["revision"] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    return prompt


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
    # Preserve interior indentation for boxed TUI menus.
    lines = [match.group(1) if (match := BOX_ROW_RE.match(line)) else line for line in lines]
    checklist = _multiselect(lines)
    if checklist:
        return checklist

    # B：编号列表（优先判，因为它能直接发数字，最稳）
    for offset in range(len(lines) - 1, -1, -1):
        raw = lines[offset]
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
            # 编号列表里同级行都带序号：下一行只要比本行行首更靠右、
            # 不是序号行/提示行，就是本项的描述
            description = _description_after(lines, position, _indent(lines[position]))
            block.append({"label": label, "meta": meta,
                          "selected": row_marker in CURSOR_MARKS,
                          "number": match.group(1),
                          "description": description})
            expected += 1
            position += 1
            if description:
                position += 1   # 描述行已归属上一项，下一轮直接看下一个序号
        if len(block) >= 2 and all(_option_is_plausible(item["label"]) for item in block):
            hint_at = _hint_index(lines, offset, position - 1, above=2, below=4)
            if hint_at is None:
                continue          # 没有提示行：多半是聊天记录里的编号列表，不是菜单
            title = _title_above(lines, min(hint_at, offset))
            hint = _clean(lines[hint_at]).strip()[:160]
            return _finish("menu", title, hint, block, lines, numbers=True, source="numbered")

    # A：光标列表
    for offset in range(len(lines) - 1, -1, -1):
        raw = lines[offset]
        line = _clean(raw)
        if _marker(line)[0] not in CURSOR_MARKS:
            continue
        collected = _collect_rows(lines, offset)[0]
        options = collected["options"]
        if len(options) < 2 or not all(_option_is_plausible(item["label"]) for item in options):
            continue
        top = collected["first_line"]
        # kimi 的信任对话框把提示贴在标题下面、选项放在屏幕底部，所以往上要看得远一些
        hint_at = _hint_index(lines, top, collected["last_line"], above=12)
        # 没有提示行的"列表"多半是 shell 提示符/聊天正文，宁可不出卡片
        if hint_at is None:
            continue
        # 标题在「提示行」或「选项块」两者更靠上的那个的上面：kimi 是提示在标题下方、
        # 选项在底部；claude 是选项紧跟标题、提示行在最后
        return _finish("menu", _title_above(lines, min(hint_at, top)),
                       _clean(lines[hint_at]).strip()[:160],
                       options, lines, numbers=collected["numbers"], source="cursor")

    # C：确认类。对话框一定贴着屏幕底部，所以只看最后 6 行非空内容：
    # 聊天/终端里被回显的命令、diff 常常写着 "press Enter to continue"，位置高得多。
    confirm_tail = max(0, len(lines) - 6)
    for offset in range(len(lines) - 1, confirm_tail - 1, -1):
        raw = lines[offset]
        line = _clean(raw).strip()
        if _looks_like_code(line):
            continue          # 源码/命令回显里的 "press enter to continue" 不是对话框
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

    # 屏幕上只有开关（没有列表/确认）：也给一张卡，只要它有 ←→ 提示
    toggles = _toggle_rows(lines)
    if toggles and any(toggle.get("hint") for toggle in toggles):
        first = next(toggle for toggle in toggles if toggle.get("hint"))
        return _finish("toggle", first["label"], first["hint"], [], lines,
                       numbers=False, source="toggle")
    return None


def choice_keys(prompt: dict[str, Any], index: int) -> list[str]:
    """Translate a visible option into keys from the captured cursor position."""
    options = prompt.get("options") or []
    if not options:
        raise ValueError("no options")
    index = int(index)
    if not 0 <= index < len(options):
        raise ValueError("option index is out of range")
    option = options[index]
    if prompt.get("source") == "codex_command_approval":
        if prompt.get("verified") and not prompt.get("read_only"):
            if index == 0:
                return ["y"]
            if index == len(options) - 1:
                return ["Escape"]
        raise ValueError("请在 Codex 原生界面确认此选项")
    if prompt.get("source") == "hermes_clarify":
        return [option["number"]]
    if option.get("key"):
        return {"yes": ["y", "Enter"], "no": ["n", "Enter"],
                "enter": ["Enter"], "esc": ["Escape"]}[option["key"]]
    if prompt.get("cursor"):
        delta = index - int(prompt["selected"])
        action = "Space" if prompt.get("kind") == "multi" else "Enter"
        return ["Down" if delta > 0 else "Up"] * abs(delta) + [action]
    if prompt.get("numbers") and option.get("number"):
        return [option["number"], "Enter"]
    raise ValueError("menu has no supported navigation")



def choice_for_key(prompt: dict[str, Any], key: str) -> list[str] | None:
    """Esc/Enter 之类的直接按键；也允许按序号选择。"""
    key = (key or "").lower()
    if key in ("esc", "escape", "cancel"):
        return ["Escape"]
    if key in ("enter", "confirm", "ok"):
        return ["Enter"]
    if prompt.get("kind") == "multi":
        return None
    if key in ("yes", "y"):
        return ["y", "Enter"]
    if key in ("no", "n"):
        return ["n", "Enter"]
    if key.isdigit():
        return choice_keys(prompt, int(key) - 1)
    return None


def menu_signature(prompt: dict) -> str:
    """同一道题的指纹：标题 + 选项标签 + 开关标签/当前值。

    只用于"这道题你授权过没有"，不参与任何解析判断 —— 解析照旧 fail-closed。
    """
    if not prompt:
        return ""
    parts = [str(prompt.get("title") or "")]
    for option in prompt.get("options") or []:
        parts.append(f"o:{option.get('label', '')}")
    for toggle in prompt.get("toggles") or []:
        parts.append(f"t:{toggle.get('label', '')}={toggle.get('active')}")
    raw = "\u0000".join(parts).strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
