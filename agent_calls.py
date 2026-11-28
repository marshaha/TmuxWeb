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

import math
import os
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

STATUSES = ("running", "ok", "timeout", "error")
MAX_TEXT = 20_000
RECEIPT_WINDOW = 600


def message_time(value):
    try:
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000
    except (ValueError, TypeError, OverflowError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return None
            seconds = stamp.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def receipt_updates(rows, turns):
    """A call and a recorded user turn must each have exactly one candidate."""
    by_text = defaultdict(list)
    for row in rows:
        by_text[row["question"].strip().replace("\r\n", "\n")].append(row)
    candidates = defaultdict(list)
    for mid, (text, ts) in turns.items():
        matches = []
        for row in by_text.get(text, ()):
            # Some harnesses record only whole seconds.
            earliest = int(row["created"]) if ts.is_integer() else row["created"]
            if earliest <= ts <= row["created"] + RECEIPT_WINDOW:
                matches.append(row)
        for row in matches:
            candidates[row["id"]].append((mid, ts, len(matches)))
    for call_id, matches in candidates.items():
        mid, ts, count = matches[0]
        if len(matches) == 1 and count == 1:
            yield mid, ts, call_id


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
    progress: str = ""
    deadline: float = 0
    received_at: float = 0

    @property
    def duration(self) -> float:
        end = self.finished or time.time()
        return round(max(0.0, end - self.created), 2)

    def as_dict(self, full: bool = False) -> dict:
        data = {
            "id": self.id, "parent": self.parent, "child": self.child,
            "status": self.status, "source": self.source,
            "created": self.created, "finished": self.finished,
            "duration": self.duration, "error": self.error, "progress": self.progress,
            "question": self.question if full else self.question[:280],
        }
        if full:
            data["reply"] = self.reply
        else:
            data["reply_preview"] = self.reply[:280]
        if self.received_at:
            data["received_at"] = self.received_at
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
            # Serialize migrations across workers opening a pre-receipt database.
            self._connection.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in self._connection.execute("PRAGMA table_info(calls)")}
            if "progress" not in columns:
                self._connection.execute("ALTER TABLE calls ADD COLUMN progress TEXT NOT NULL DEFAULT ''")
            if "deadline" not in columns:
                self._connection.execute("ALTER TABLE calls ADD COLUMN deadline REAL NOT NULL DEFAULT 0")
            if "received_at" not in columns:
                self._connection.execute("ALTER TABLE calls ADD COLUMN received_at REAL NOT NULL DEFAULT 0")
                self._connection.execute("ALTER TABLE calls ADD COLUMN receipt_session TEXT")
                self._connection.execute("ALTER TABLE calls ADD COLUMN receipt_message TEXT")
            self._connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS calls_receipt ON "
                                     "calls(child, receipt_session, receipt_message) WHERE received_at>0")
            self._connection.execute("CREATE INDEX IF NOT EXISTS calls_unreceived ON calls(created, child) "
                                     "WHERE source='message' AND parent!='' AND received_at=0 AND progress='submitted'")
            self._connection.execute("CREATE INDEX IF NOT EXISTS calls_parent ON calls (parent, created)")
            self._connection.execute("CREATE INDEX IF NOT EXISTS calls_child ON calls (child, created)")
            self._connection.commit()

    def start(self, parent: str, child: str, question: str, source: str = "", timeout: float = 0) -> Call:
        call = Call(id=uuid.uuid4().hex[:12], parent=parent or "", child=child or "",
                    question=(question or "")[:MAX_TEXT], reply="", status="running",
                    source=source or "", created=time.time(), finished=0.0, error="")
        call.deadline = call.created + timeout if timeout else 0
        with self._lock:
            self._connection.execute(
                "INSERT INTO calls (id,parent,child,question,reply,status,source,created,finished,error,deadline)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (call.id, call.parent, call.child, call.question, "", "running",
                 call.source, call.created, 0.0, "", call.deadline))
            self._connection.commit()
        return call

    def progress(self, call_id, stage):
        with self._lock, self._connection:
            self._connection.execute("UPDATE calls SET progress=? WHERE id=? AND progress!=?",
                                     (stage, call_id, stage))

    def outbound(self, parent, limit=8):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM calls WHERE parent=? AND created>=? ORDER BY created DESC LIMIT ?",
                (parent, time.time()-86400, limit)).fetchall()
        return [self._call(row).as_dict() for row in rows]

    def pending_recipients(self):
        with self._lock:
            return dict(self._connection.execute(
                "SELECT child, MAX(created) FROM calls WHERE source='message' AND parent!='' "
                "AND progress='submitted' AND received_at=0 AND created>=? GROUP BY child",
                (time.time()-86400,)))

    def match_receipts(self, child, transcript):
        """Correlate whole user turns, then return verified routes for this transcript page."""
        session = transcript.get("session_id")
        if not session:
            return {}
        with self._lock:
            if not self._connection.execute(
                    "SELECT 1 FROM calls WHERE child=? AND source='message' AND parent!='' LIMIT 1",
                    (child,)).fetchone():
                return {}
        turns = {}
        for message in transcript.get("messages") or ():
            if message.get("role") != "user" or message.get("id") is None:
                continue
            ts = message_time(message.get("timestamp"))
            if ts is not None:
                text = str(message.get("text") or "").strip().replace("\r\n", "\n")
                turns[str(message["id"])] = (text, ts)
        if not turns:
            return {}
        slots = ",".join("?" for _ in turns)
        lookup = ("SELECT receipt_message, question, received_at, parent, child, id FROM calls "
                  f"WHERE child=? AND receipt_session=? AND received_at>0 AND receipt_message IN ({slots})")
        keys = (child, session, *turns)

        def linked():
            result = {}
            for row in self._connection.execute(lookup, keys):
                mid = row["receipt_message"]
                # Some legacy adapters generate IDs from a row's position in the page.
                if turns[mid] == (row["question"].strip().replace("\r\n", "\n"), row["received_at"]):
                    result[mid] = {"parent": row["parent"], "child": row["child"], "id": row["id"]}
            return result

        with self._lock, self._connection:
            existing = linked()
            fresh = {mid: turn for mid, turn in turns.items() if mid not in existing}
            if not fresh:
                return existing
            times = [ts for _, ts in fresh.values()]
            rows = self._connection.execute(
                "SELECT id, question, created FROM calls WHERE child=? AND source='message' AND parent!='' "
                "AND progress='submitted' AND received_at=0 AND created BETWEEN ? AND ? LIMIT 513",
                (child, min(times)-RECEIPT_WINDOW, max(times)+1)).fetchall()
            if not rows or len(rows) > 512:
                return existing
            changes = [(session, mid, ts, call_id) for mid, ts, call_id in receipt_updates(rows, fresh)]
            if not changes:
                return existing
            self._connection.executemany(
                "UPDATE OR IGNORE calls SET receipt_session=?, receipt_message=?, received_at=? "
                "WHERE id=? AND received_at=0", changes)
            return linked()

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

    def relationships(self, aliases: set[str], limit: int) -> list[dict]:
        """One active or most recent assignment per peer and direction."""
        refs = sorted(aliases)
        slots = ",".join("?" for _ in refs)
        query = f"""WITH edges AS (
            SELECT id, child AS peer, 'child' AS relation, status, created
            FROM calls WHERE parent IN ({slots}) AND child != ''
            UNION ALL
            SELECT id, parent AS peer, 'parent' AS relation, status, created
            FROM calls WHERE child IN ({slots}) AND parent != ''
        ), ranked AS (
            SELECT *, row_number() OVER (PARTITION BY peer, relation
                ORDER BY (status='running') DESC, created DESC, id DESC) AS rank FROM edges
        )
        SELECT calls.*, ranked.peer, ranked.relation FROM ranked JOIN calls USING(id)
        WHERE rank=1 ORDER BY (calls.status='running') DESC, calls.created DESC, calls.id DESC LIMIT ?"""
        with self._lock:
            rows = self._connection.execute(query, (*refs, *refs, max(1, min(200, limit)))).fetchall()
        return [dict(self._call(row).as_dict(), peer=row["peer"], relation=row["relation"]) for row in rows]

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
                    question=row["question"], reply=row["reply"], status=("timeout" if row["status"] == "running" and 0 < row["deadline"] < time.time()-15 else row["status"]),
                    source=row["source"], created=row["created"], finished=row["finished"],
                    error=row["error"], progress=row["progress"], deadline=row["deadline"], received_at=row["received_at"])
