#!/usr/bin/env python3
"""Translate native human-input events into pane-scoped, read-only Chat notices."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time


def text(value, size=400):
    return value.strip()[:size] if isinstance(value, str) else ""


def observation(provider, event, data):
    """Return (session, request, operation, title, detail). Store display fields only."""
    session = text(data.get("session_id") or data.get("thread_id"), 160)
    if provider == "opencode":
        items = data.get("items")
        if not isinstance(items, list) or (items and not session):
            return None
        labels = {"form": "等待填写表单", "permission": "等待工具审批"}
        rows = [(text(i.get("id"), 160), labels[i["kind"]]) for i in items[:64]
                if isinstance(i, dict) and i.get("kind") in labels and text(i.get("id"))]
        return session, "", "snapshot", "", rows
    if not session and provider != "hermes":
        return None
    request = text(data.get("tool_use_id") or data.get("tool_call_id") or data.get("call_id"), 160)
    detail = ""
    if provider == "hermes":
        extra = data.get("extra") or {}
        if extra.get("platform") not in ("cli", "tui"):
            return None
        request = text(extra.get("request_id"), 160)
        if not request:
            return None
        if event == "on_human_input_resolved":
            return session, request, "close", "", ""
        if event != "on_human_input_request":
            return None
        title = {"sudo": "需要在终端输入密码", "clarify": "需要回答问题",
                 "approval": "等待操作审批"}.get(extra.get("kind"))
        if not title:
            return None
        if extra.get("kind") == "clarify":
            detail = text(extra.get("prompt"), 1200)
    elif provider in ("claude", "codex", "kimi", "gemini"):
        if event in ("Stop", "SessionEnd", "AfterAgent", "Interrupt", "StopFailure"):
            return session, "", "clear", "", ""
        if event in ("PermissionResult", "PostToolUse", "PostToolUseFailure", "AfterTool", "ElicitationResult"):
            if event == "ElicitationResult":
                request = text(data.get("elicitation_id"), 160) or "elicitation"
            elif not request and event in ("PostToolUse", "PostToolUseFailure"):
                return None
            return session, request or "permission", "close", "", ""
        if event == "PreToolUse" and provider == "claude" and data.get("tool_name") == "AskUserQuestion":
            if not request:
                return None
            title = "需要回答问题"
            detail = questions((data.get("tool_input") or {}).get("questions"))
        elif event == "Elicitation" and provider == "claude":
            title = "MCP 请求补充信息"
            request = text(data.get("elicitation_id"), 160) or "elicitation"
            detail = text(data.get("message"))
        elif event == "PermissionRequest" or (provider == "gemini" and event == "Notification"
                                              and data.get("notification_type") == "ToolPermission"):
            title = "等待工具审批"
            detail = text(data.get("tool_name"), 100)
            request = request or "permission"
        else:
            return None
    else:
        return None
    return session, request, "open", title, detail


def questions(items):
    lines = []
    for item in items[:8] if isinstance(items, list) else ():
        if not isinstance(item, dict) or not text(item.get("question")):
            continue
        mode = "多选" if item.get("multiSelect") else "选择" if item.get("options") else "填写"
        lines.append("【" + mode + "】" + text(item["question"]))
        options = item.get("options")
        if isinstance(options, list):
            for opt in options[:12]:
                if isinstance(opt, dict) and text(opt.get("label")):
                    lines.append("  • " + text(opt["label"], 140))
    return "\n".join(lines)[:2400]


def record(path, pane, pid, provider, notice, now):
    session, request, operation, title, detail = notice
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(path, timeout=1) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS waits(
            pane TEXT, pid TEXT, provider TEXT, session TEXT, request TEXT,
            title TEXT, detail TEXT, at REAL,
            PRIMARY KEY(pane,pid,provider,session,request))""")
        db.execute("DELETE FROM waits WHERE at < ?", (now - 3600,))
        args = (pane, pid, provider, session)
        if operation == "snapshot":
            db.execute("DELETE FROM waits WHERE pane=? AND pid=? AND provider=?", args[:3])
            db.executemany("INSERT OR REPLACE INTO waits VALUES(?,?,?,?,?,?,?,?)",
                           [(*args, key, label, "", now) for key, label in detail])
        else:
            # Keep close timestamps: a slow request hook may obtain its tmux
            # identity after the result hook has already written to the DB.
            if operation == "clear":
                db.execute("DELETE FROM waits WHERE pane=? AND pid=? AND provider=? AND session=? AND at<=?",
                           (*args, now))
            if operation == "open":
                barrier = db.execute("SELECT at FROM waits WHERE pane=? AND pid=? AND provider=? "
                                     "AND session=? AND request=''", args).fetchone()
                if barrier and barrier[0] >= now:
                    return
            db.execute("""INSERT INTO waits VALUES(?,?,?,?,?,?,?,?)
                          ON CONFLICT(pane,pid,provider,session,request) DO UPDATE SET
                          title=excluded.title, detail=excluded.detail, at=excluded.at
                          WHERE excluded.at >= waits.at""",
                       (*args, request, title, detail, now))


def main():
    received = time.time()
    pane = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%[0-9]+", pane) or not os.environ.get("TMUX") or len(sys.argv) != 2:
        return
    data = json.load(sys.stdin)
    notice = observation(sys.argv[1], data.get("hook_event_name"), data)
    if notice is None:
        return
    pid = subprocess.check_output(["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
                                  stderr=subprocess.DEVNULL, timeout=1, text=True).strip()
    if not pid.isdigit():
        return
    path = Path.home() / ".config" / "tmuxweb-agent" / "interactions.sqlite3"
    os.umask(0o077)
    record(path, pane, pid, sys.argv[1], notice, received)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError):
        pass  # An observer must not interrupt or answer the harness.
