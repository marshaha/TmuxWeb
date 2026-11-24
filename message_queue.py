"""A durable per-pane message queue.

Why it exists: sending into a busy agent used to rely on the paste-Enter dance and
a background retry that lived only in memory — restart the server (or reload the
page) and the pending message was gone. This store keeps the message until a
worker has actually delivered it, so "排队，等这一轮结束再发" survives restarts.

Modes
    queue   wait for the pane to go idle, then send.
    steer   interrupt the running turn (Escape) and send right away.

States
    pending → sending → sent
                      ↘ failed (after max_attempts) or cancelled by the user

A row stuck in `sending` when the process died is put back to `pending` on start.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field

MODES = ("queue", "steer")
STATES = ("pending", "sending", "sent", "failed", "cancelled")
MAX_ATTEMPTS = 3


def _now() -> float:
    return time.time()


@dataclass
class QueueItem:
    id: str
    pane: str
    text: str
    submit: bool
    mode: str
    state: str
    attempts: int
    created: float
    updated: float
    error: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "pane": self.pane, "text": self.text, "submit": self.submit,
            "mode": self.mode, "state": self.state, "attempts": self.attempts,
            "created": self.created, "updated": self.updated, "error": self.error,
        }


class QueueStore:
    """SQLite-backed queue. All methods are thread-safe (a lock around one connection)."""

    def __init__(self, path: str = "") -> None:
        self.path = path or os.environ.get("TMUX_WEB_QUEUE_DB") or "message_queue.db"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS pane_queue (
                       id TEXT PRIMARY KEY,
                       pane TEXT NOT NULL,
                       text TEXT NOT NULL,
                       submit INTEGER NOT NULL DEFAULT 1,
                       mode TEXT NOT NULL DEFAULT 'queue',
                       state TEXT NOT NULL DEFAULT 'pending',
                       attempts INTEGER NOT NULL DEFAULT 0,
                       created REAL NOT NULL,
                       updated REAL NOT NULL,
                       error TEXT NOT NULL DEFAULT ''
                   )"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS pane_queue_pane ON pane_queue (pane, state, created)")
            self._connection.commit()

    # -- writing ---------------------------------------------------------
    def add(self, pane: str, text: str, submit: bool = True, mode: str = "queue") -> QueueItem:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        if not str(text or "").strip():
            raise ValueError("空的排队消息")
        item = QueueItem(id=uuid.uuid4().hex[:12], pane=pane, text=str(text), submit=bool(submit),
                         mode=mode, state="pending", attempts=0, created=_now(), updated=_now())
        with self._lock:
            self._connection.execute(
                "INSERT INTO pane_queue (id,pane,text,submit,mode,state,attempts,created,updated,error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item.id, item.pane, item.text, int(item.submit), item.mode, item.state,
                 item.attempts, item.created, item.updated, ""))
            self._connection.commit()
        return item

    def claim_next(self, pane: str) -> QueueItem | None:
        """Take the oldest pending item for a pane and mark it `sending`."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM pane_queue WHERE pane=? AND state='pending'"
                " ORDER BY created ASC LIMIT 1", (pane,)).fetchone()
            if row is None:
                return None
            self._connection.execute(
                "UPDATE pane_queue SET state='sending', attempts=attempts+1, updated=? WHERE id=?",
                (_now(), row["id"]))
            self._connection.commit()
            row = self._connection.execute("SELECT * FROM pane_queue WHERE id=?",
                                           (row["id"],)).fetchone()
            return self._item(row)

    def finish(self, item_id: str, state: str, error: str = "") -> None:
        if state not in STATES:
            raise ValueError(f"unknown state: {state}")
        with self._lock:
            self._connection.execute(
                "UPDATE pane_queue SET state=?, error=?, updated=? WHERE id=?",
                (state, error or "", _now(), item_id))
            self._connection.commit()

    def retry_later(self, item_id: str, error: str = "") -> QueueItem | None:
        """Put a claimed item back to pending, unless it has used up its attempts."""
        with self._lock:
            row = self._connection.execute("SELECT * FROM pane_queue WHERE id=?",
                                           (item_id,)).fetchone()
            if row is None:
                return None
            if row["attempts"] >= MAX_ATTEMPTS:
                self._connection.execute(
                    "UPDATE pane_queue SET state='failed', error=?, updated=? WHERE id=?",
                    (error or "重试次数用完", _now(), item_id))
                state = "failed"
            else:
                self._connection.execute(
                    "UPDATE pane_queue SET state='pending', error=?, updated=? WHERE id=?",
                    (error or "", _now(), item_id))
                state = "pending"
            self._connection.commit()
            if state == "failed":
                return None
            return self._item(row)

    def set_mode(self, item_id: str, mode: str) -> bool:
        """还没发出去的条目改投递方式（排队 ↔ 打断并立刻发）。"""
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE pane_queue SET mode=?, updated=? WHERE id=? AND state='pending'",
                (mode, _now(), item_id))
            self._connection.commit()
            return cursor.rowcount > 0

    def cancel(self, item_id: str, pane: str | None = None) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE pane_queue SET state='cancelled', updated=? WHERE id=? "
                "AND state IN ('pending','sending','failed') AND (? IS NULL OR pane=?)",
                (_now(), item_id, pane, pane))
            self._connection.commit()
            return cursor.rowcount > 0

    def reset_inflight(self) -> int:
        """Process restart: anything mid-flight goes back to pending."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE pane_queue SET state='pending', updated=? WHERE state='sending'", (_now(),))
            self._connection.commit()
            return cursor.rowcount

    # -- reading ---------------------------------------------------------
    def list(self, pane: str = "", include_done: bool = False) -> list[QueueItem]:
        query = "SELECT * FROM pane_queue"
        clauses, params = [], []
        if pane:
            clauses.append("pane=?")
            params.append(pane)
        if not include_done:
            clauses.append("state IN ('pending','sending','failed')")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created ASC"
        with self._lock:
            return [self._item(row) for row in self._connection.execute(query, params)]

    def panes_with_pending(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT pane FROM pane_queue WHERE state='pending'").fetchall()
            return [row["pane"] for row in rows]

    def get(self, item_id: str) -> QueueItem | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM pane_queue WHERE id=?",
                                           (item_id,)).fetchone()
            return self._item(row) if row else None

    @staticmethod
    def _item(row: sqlite3.Row) -> QueueItem:
        return QueueItem(id=row["id"], pane=row["pane"], text=row["text"], submit=bool(row["submit"]),
                         mode=row["mode"], state=row["state"], attempts=row["attempts"],
                         created=row["created"], updated=row["updated"], error=row["error"] or "")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
