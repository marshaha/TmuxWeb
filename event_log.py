"""A write-ahead journal for pane operations.

"Something typed into my agent and I don't know what" is the failure this
prevents. Every mutating pane operation gets a row **before** it runs, so:

* a crash mid-operation leaves a `running` row behind — proof that something was
  in flight, with its payload, instead of a silent gap;
* the history of a pane is auditable: what was sent, by whom (UI, MCP relay, the
  queue worker), when, and whether it succeeded;
* `sweep()` turns rows abandoned by a restart into `unknown` instead of leaving
  them looking live forever.

The payload is truncated (default 4000 chars) — enough to recognise a message,
not a place to keep transcripts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

STATUSES = ("running", "ok", "error", "unknown", "retrying")
MAX_PAYLOAD = 4000


@dataclass
class Event:
    id: str
    at: float
    kind: str
    pane: str
    actor: str
    status: str
    request: str          # JSON text
    result: str           # JSON text
    error: str
    finished: float

    @property
    def duration(self) -> float:
        if not self.finished:
            return round(max(0.0, time.time() - self.at), 2)
        return round(max(0.0, self.finished - self.at), 2)

    def as_dict(self, full: bool = False) -> dict:
        data = {
            "id": self.id, "at": self.at, "kind": self.kind, "pane": self.pane,
            "actor": self.actor, "status": self.status, "error": self.error,
            "duration": self.duration,
            "summary": _summary(self.kind, self.request),
        }
        if full:
            data["request"] = _loads(self.request)
            data["result"] = _loads(self.result)
        return data


def _loads(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _summary(kind: str, request: str) -> str:
    """One line the drawer can show without expanding: what was actually sent."""
    payload = _loads(request)
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "key", "command", "name", "target", "path", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            first = value.strip().splitlines()[0]
            return first[:140]
    return ""


class EventLog:
    def __init__(self, path: str = "", keep_days: int = 14):
        self.path = path or os.environ.get("TMUX_WEB_EVENTS_DB") or "events.db"
        self.keep_days = keep_days
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS events (
                       id TEXT PRIMARY KEY,
                       at REAL NOT NULL,
                       kind TEXT NOT NULL,
                       pane TEXT NOT NULL DEFAULT '',
                       actor TEXT NOT NULL DEFAULT '',
                       status TEXT NOT NULL DEFAULT 'running',
                       request TEXT NOT NULL DEFAULT '',
                       result TEXT NOT NULL DEFAULT '',
                       error TEXT NOT NULL DEFAULT '',
                       finished REAL NOT NULL DEFAULT 0
                   )"""
            )
            self._connection.execute("CREATE INDEX IF NOT EXISTS events_pane ON events (pane, at)")
            self._connection.execute("CREATE INDEX IF NOT EXISTS events_status ON events (status)")
            self._connection.commit()
        self.sweep()

    def begin(self, kind: str, pane: str = "", actor: str = "ui",
              request: dict | None = None) -> Event:
        """写盘之后才允许调用方真的去做那件事。"""
        if not kind:
            raise ValueError("kind is required")
        event = Event(id=uuid.uuid4().hex[:12], at=time.time(), kind=kind, pane=pane or "",
                      actor=actor or "", status="running",
                      request=json.dumps(request or {}, ensure_ascii=False)[:MAX_PAYLOAD],
                      result="", error="", finished=0.0)
        with self._lock:
            self._connection.execute(
                "INSERT INTO events (id,at,kind,pane,actor,status,request,result,error,finished)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event.id, event.at, event.kind, event.pane, event.actor, "running",
                 event.request, "", "", 0.0))
            self._connection.commit()
        return event

    def finish(self, event_id: str, status: str = "ok", result: dict | None = None,
               error: str = "") -> Event | None:
        if status not in STATUSES or status == "running":
            raise ValueError(f"bad terminal status: {status}")
        with self._lock:
            self._connection.execute(
                "UPDATE events SET status=?, result=?, error=?, finished=? WHERE id=?",
                (status, json.dumps(result or {}, ensure_ascii=False)[:MAX_PAYLOAD],
                 (error or "")[:1000], time.time(), event_id))
            self._connection.commit()
        return self.get(event_id)

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def recent(self, pane: str = "", kind: str = "", limit: int = 50,
               kinds: tuple[str, ...] = ()) -> list[Event]:
        limit = max(1, min(int(limit), 500))
        query = "SELECT * FROM events WHERE 1=1"
        params: list = []
        if pane:
            query += " AND pane=?"
            params.append(pane)
        if kind:
            query += " AND kind=?"
            params.append(kind)
        if kinds:
            query += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
            params.extend(kinds)
        query += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._event(row) for row in rows]

    def inflight(self) -> list[Event]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE status IN ('running','retrying') ORDER BY at ASC").fetchall()
        return [self._event(row) for row in rows]

    def sweep(self, stale_after: float = 300.0) -> int:
        """Restart 之后仍挂着的行不可能还在跑 —— 标成 unknown，别骗人。"""
        cutoff = time.time() - max(1.0, stale_after)
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE events SET status='unknown', finished=? "
                "WHERE status IN ('running','retrying') AND at < ?", (time.time(), cutoff))
            self._connection.commit()
            return cursor.rowcount

    def prune(self) -> int:
        cutoff = time.time() - self.keep_days * 86400
        with self._lock:
            cursor = self._connection.execute("DELETE FROM events WHERE at < ?", (cutoff,))
            self._connection.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(id=row["id"], at=row["at"], kind=row["kind"], pane=row["pane"],
                     actor=row["actor"], status=row["status"], request=row["request"],
                     result=row["result"], error=row["error"], finished=row["finished"])
