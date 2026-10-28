"""Cross-server tmux relay.

One TmuxWeb instance can see panes on every host it manages (local + SSH). This
module turns that into an agent-to-agent message bus: inject a question into a
pane on any host, wait for the agent to finish, and return its answer to the
caller — including callers on a different server.

The reply protocol is deliberately transport-agnostic: it only needs
``capture()`` and ``send()``, so the same code drives a local ``tmux`` call and
a pane behind SSH.
"""
from __future__ import annotations

import json
import re
import secrets
import time

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PROMPT_LINE_RE = re.compile(r"^\s*[❯›>]\s*\S?")
BARE_PROMPT_RE = re.compile(r"^\s*[$#%❯›>]\s*$")
SHELL_PROMPT_RE = re.compile(r"^\s*(?:\([^)]*\)\s*)?[^\s@]+@[^\s:]+[^\s]*[$#]\s*$")
# Shown only while a turn runs, next to the interrupt key: reliable.
BUSY_HINTS = (
    "esc to interrupt", "esc to cancel", "ctrl+c to cancel", "ctrl-c to cancel",
    "esc·interrupt", "esc interrupt",
)
# Progress wording is only trusted with an ellipsis and only near the bottom: status
# bars say things like "K3.1 Pro thinking: high" while the agent sits idle.
SOFT_BUSY_HINTS = (
    "thinking…", "thinking...", "generating…", "generating...", "working…", "working...",
    "compacting…", "compacting...", "running…", "running...", "waiting for",
)
# An agent's input box ("│ > 拉取新的pr") also means "ready for input".
PROMPT_BOX_RE = re.compile(r"^\s*[│|]\s*[>❯›]\s*\S?")
SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷"
REPLY_PREFIX = "TMUXWEB_REPLY_"


class RelayError(RuntimeError):
    pass


class RelayBusy(RelayError):
    pass


class RelayTimeout(RelayError):
    def __init__(self, message: str, tail: str = ""):
        super().__init__(message)
        self.tail = tail


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def classify(text: str) -> str:
    """Best-effort agent state from the visible tail: idle, busy or unknown.

    Ordering matters: an explicit interrupt hint beats everything, a spinner only
    counts near the bottom (scrollback is full of old frames), progress wording needs
    an ellipsis, and anything unmatched is *unknown* rather than a fake "busy".
    """
    tail = [line.rstrip() for line in strip_ansi(text or "").splitlines() if line.strip()][-14:]
    if not tail:
        return "unknown"
    joined = "\n".join(tail).lower()
    if any(hint in joined for hint in BUSY_HINTS):
        return "busy"
    if any(char in line for line in tail[-3:] for char in SPINNER_CHARS):
        return "busy"
    for line in tail[-3:]:
        lowered = line.strip().lower()
        if any(hint in lowered for hint in SOFT_BUSY_HINTS):
            return "busy"
    for line in tail[-4:]:
        stripped = line.strip()
        if (PROMPT_LINE_RE.match(stripped) or BARE_PROMPT_RE.match(stripped)
                or SHELL_PROMPT_RE.match(stripped) or PROMPT_BOX_RE.match(line)):
            return "idle"
    return "unknown"


def marker(token: str) -> str:
    return f"{REPLY_PREFIX}{token}:"


def build_prompt(question: str, token: str, source: str = "") -> str:
    origin = f" 来自 {source} 的 TmuxWeb 中继请求" if source else " TmuxWeb 中继请求"
    return (
        f"[TMUXWEB_RELAY{origin}] 请直接回答下面的问题，不要调用任何消息或邮箱工具：\n"
        f"{question}\n\n"
        "回答完成后，在同一次回复的最后单独写一行：先写标记\n"
        f"{marker(token)}\n"
        "再在同一行紧跟你的完整回答，把回答写成一个 JSON 字符串（一对英文双引号，内部引号与换行要转义）。"
        "标记和 JSON 必须写在同一行，不要使用 Markdown 代码块。"
    )


def extract_reply(text: str, token: str, ignore: set[str] | None = None) -> str | None:
    """Return the last valid marked reply in a pane capture, if any.

    The injected prompt also mentions the marker, so instruction lines are
    skipped, only JSON values (or a quoted remainder) are accepted, and the
    newest match wins.
    """
    prefix = marker(token)
    decoder = json.JSONDecoder()
    found: str | None = None
    for line in strip_ansi(text or "").splitlines():
        if ignore and line.strip() in ignore:
            continue
        position = line.find(prefix)
        if position < 0:
            continue
        candidate = line[position + len(prefix):].strip().strip("`*_ ")
        if not candidate:
            continue
        value = None
        try:
            parsed, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, str):
            value = parsed
        elif isinstance(parsed, dict):
            for key in ("reply", "answer", "message", "response", "result", "text"):
                if isinstance(parsed.get(key), str):
                    value = parsed[key]
                    break
        elif parsed is None and candidate[:1] in {'"', "'"}:
            value = candidate.strip('"\'')
        if value:
            found = value
    return found


def inspect(transport, pane_id: str, provider: str | None = None, lines: int = 60) -> dict:
    text = transport.capture(pane_id, lines)
    return {"pane": pane_id, "provider": provider, "status": classify(text), "tail": strip_ansi(text)}


def ask(transport, pane_id: str, question: str, *, source: str = "", provider: str | None = None,
        timeout: int = 180, poll_interval: float = 0.8, force: bool = False) -> dict:
    question = (question or "").strip()
    if not question:
        raise RelayError("question must not be empty")
    timeout = max(5, min(int(timeout), 600))
    state = inspect(transport, pane_id, provider)
    if state["status"] == "busy" and not force:
        raise RelayBusy(f"{pane_id} is busy; pass force=true only after inspecting it")
    token = secrets.token_hex(12)
    started = time.monotonic()
    prompt = build_prompt(question, token, source)
    # The instruction lines contain the marker; never mistake them for a reply.
    instruction_lines = {line.strip() for line in prompt.splitlines() if line.strip()}
    transport.send(pane_id, prompt, True)
    deadline = started + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        text = transport.capture(pane_id, 1200)
        reply = extract_reply(text, token, instruction_lines)
        if reply is not None:
            return {"reply": reply, "pane": pane_id, "token": token, "source": source,
                    "elapsed": round(time.monotonic() - started, 2)}
    raise RelayTimeout(
        f"timed out after {timeout}s waiting for {pane_id} to print {marker(token)}",
        tail=strip_ansi(transport.capture(pane_id, 60)),
    )
