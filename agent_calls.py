"""Who asked whom, for how long, and what came back.

An agent asking another agent (relay `ask`) is a two-pane conversation: the
parent pane runs a CLI that called the MCP tool, the child pane is the agent that
answers. Nothing recorded that edge before, so "which agent is this result from,
and is it still waiting?" was answerable only by watching screens.

Rows are edges, not a single flat log:

    parent   pane id of the caller (empty when the caller is a human / curl)
    child    pane id that was asked
    status   running | ok | timeout | error
    question / reply   full text, so a result can be read without the transcript

The tree endpoint walks parent → child, so a chain A→B→C renders as a tree. A
cycle (A asks B, B asks A) is cut by the visited set — that happens for real when
two agents decide to consult each other.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

STATUSES = ("running", "ok", "timeout", "error")
MAX_TEXT = 20_000


@dataclass
class Call:
    id: str
    parent: str
    child: str
    question: str
    reply: str
    status: str
    source: str
    created: float
    finished: float
    error: str

    @property
    def duration(self) -> float:
        end = self.finished or time.time()
        return round(max(0.0, end - self.created), 2)

    def as_dict(self, full: bool = False) -> dict:
        data = {
            "id": self.id, "parent": self.parent, "child": self.child,
            "status": self.status, "source": self.source,
            "created": self.created, "finished": self.finished,
            "duration": self.duration, "error": self.error,
            "question": self.question if full else self.question[:280],
        }
        if full:
            data["reply"] = self.reply
        else:
            data["reply_preview"] = self.reply[:280]
        return data


class AgentCallStore:
    def __init__(self, path: str = ""):
        self.path = path or os.environ.get("TMUX_WEB_CALLS_DB") or "agent_calls.db"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS calls (
                       id TEXT PRIMARY KEY,
                       parent TEXT NOT NULL DEFAULT '',
                       child TEXT NOT NULL DEFAULT '',
                       question TEXT NOT NULL DEFAULT '',
                       reply TEXT NOT NULL DEFAULT '',
                       status TEXT NOT NULL DEFAULT 'running',
                       source TEXT NOT NULL DEFAULT '',
                       created REAL NOT NULL,
                       finished REAL NOT NULL DEFAULT 0,
                       error TEXT NOT NULL DEFAULT ''
                   )"""
            )
            self._connection.execute("CREATE INDEX IF NOT EXISTS calls_parent ON calls (parent, created)")
            self._connection.execute("CREATE INDEX IF NOT EXISTS calls_child ON calls (child, created)")
            self._connection.commit()

    def start(self, parent: str, child: str, question: str, source: str = "") -> Call:
        call = Call(id=uuid.uuid4().hex[:12], parent=parent or "", child=child or "",
                    question=(question or "")[:MAX_TEXT], reply="", status="running",
                    source=source or "", created=time.time(), finished=0.0, error="")
        with self._lock:
            self._connection.execute(
                "INSERT INTO calls (id,parent,child,question,reply,status,source,created,finished,error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (call.id, call.parent, call.child, call.question, "", "running",
                 call.source, call.created, 0.0, ""))
            self._connection.commit()
        return call

    def finish(self, call_id: str, reply: str = "", status: str = "ok",
               error: str = "") -> Call | None:
        if status not in STATUSES or status == "running":
            raise ValueError(f"bad terminal status: {status}")
        with self._lock:
            self._connection.execute(
                "UPDATE calls SET reply=?, status=?, error=?, finished=? WHERE id=?",
                ((reply or "")[:MAX_TEXT], status, error or "", time.time(), call_id))
            self._connection.commit()
        return self.get(call_id)

    def get(self, call_id: str) -> Call | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        return self._call(row) if row else None

    def recent(self, pane: str = "", limit: int = 50) -> list[Call]:
        """Calls that touched this pane (either side), newest first."""
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if pane:
                rows = self._connection.execute(
                    "SELECT * FROM calls WHERE parent=? OR child=? ORDER BY created DESC LIMIT ?",
                    (pane, pane, limit)).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM calls ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self._call(row) for row in rows]

    def tree(self, pane: str, depth: int = 3, limit: int = 50) -> dict:
        """Children of `pane`, recursively, with cycles cut."""
        seen = {pane}
        def walk(node: str, level: int) -> list[dict]:
            if level > max(1, depth):
                return []
            children = []
            with self._lock:
                rows = self._connection.execute(
                    "SELECT * FROM calls WHERE parent=? ORDER BY created ASC LIMIT ?",
                    (node, max(1, min(int(limit), 200)))).fetchall()
            for row in rows:
                call = self._call(row)
                entry = call.as_dict()
                if call.child and call.child not in seen:
                    seen.add(call.child)
                    entry["children"] = walk(call.child, level + 1)
                else:
                    entry["children"] = []
                children.append(entry)
            return children
        return {"pane": pane, "children": walk(pane, 1)}

    def open_calls(self) -> list[Call]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM calls WHERE status='running' ORDER BY created ASC").fetchall()
        return [self._call(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _call(row: sqlite3.Row) -> Call:
        return Call(id=row["id"], parent=row["parent"], child=row["child"],
                    question=row["question"], reply=row["reply"], status=row["status"],
                    source=row["source"], created=row["created"], finished=row["finished"],
                    error=row["error"])
