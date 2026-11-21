"""Remembered approvals for the interactive-menu card.

The card already turns a TUI menu into buttons; what was missing is memory:
clicking "允许" ten times in one turn is the same decision ten times. This store
keeps that decision, scoped, so the card can offer (or take) it again.

Scopes
    turn     valid for the rest of the current turn (a new user message ends it)
    always   valid until the user revokes it

Safety rules baked in here, not in the UI
    * nothing is remembered unless the caller passes a signature — the fingerprint
      of the menu's title, options and toggles. A different question never matches.
    * `turn` grants carry the turn key they were made in; a stale turn never matches.
    * an optional ttl caps even `always` grants (default 7 days), because a menu
      that meant "allow this one file" may not mean the same thing next week.
    * `scope_text` is stored so the UI can show exactly what was allowed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

SCOPES = ("turn", "always")
DEFAULT_TTL = 7 * 24 * 3600


@dataclass
class Grant:
    id: str
    pane: str
    signature: str
    action: str          # JSON: {"index": 2} / {"key": "enter"} / {"toggle": [0, 1]}
    scope: str
    turn: str
    label: str
    created: float
    expires: float
    uses: int

    def as_dict(self) -> dict:
        return {
            "id": self.id, "pane": self.pane, "signature": self.signature,
            "action": self.action, "scope": self.scope, "turn": self.turn,
            "label": self.label, "created": self.created, "expires": self.expires,
            "uses": self.uses,
        }


class ApprovalStore:
    def __init__(self, path: str = ""):
        self.path = path or os.environ.get("TMUX_WEB_APPROVALS_DB") or "approvals.db"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS approvals (
                       id TEXT PRIMARY KEY,
                       pane TEXT NOT NULL,
                       signature TEXT NOT NULL,
                       action TEXT NOT NULL,
                       scope TEXT NOT NULL DEFAULT 'turn',
                       turn TEXT NOT NULL DEFAULT '',
                       label TEXT NOT NULL DEFAULT '',
                       created REAL NOT NULL,
                       expires REAL NOT NULL,
                       uses INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS approvals_key ON approvals (pane, signature)")
            self._connection.commit()

    def remember(self, pane: str, signature: str, action: dict, scope: str = "turn",
                 turn: str = "", label: str = "", ttl: int = DEFAULT_TTL) -> Grant:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope}")
        if not signature:
            raise ValueError("没有菜单指纹就不记事（否则会把别的题当成同一题）")
        now = time.time()
        grant = Grant(id=uuid.uuid4().hex[:12], pane=pane, signature=signature,
                      action=json.dumps(action, ensure_ascii=False), scope=scope, turn=turn,
                      label=label, created=now, expires=now + max(60, int(ttl)), uses=0)
        with self._lock:
            # 同一个菜单同一个 pane 只留最新一条：旧的是上一个问题/上一轮，留着会误命中
            self._connection.execute("DELETE FROM approvals WHERE pane=? AND signature=?",
                                     (pane, signature))
            self._connection.execute(
                "INSERT INTO approvals (id,pane,signature,action,scope,turn,label,created,expires,uses)"
                " VALUES (?,?,?,?,?,?,?,?,?,0)",
                (grant.id, grant.pane, grant.signature, grant.action, grant.scope,
                 grant.turn, grant.label, grant.created, grant.expires))
            self._connection.commit()
        return grant

    def find(self, pane: str, signature: str, turn: str = "") -> Grant | None:
        """Which grant (if any) covers this menu right now?"""
        now = time.time()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM approvals WHERE pane=? AND signature=? AND expires>?",
                (pane, signature, now)).fetchall()
        for row in rows:
            grant = self._grant(row)
            if grant.scope == "always" or (grant.scope == "turn" and grant.turn == turn):
                return grant
        return None

    def mark_used(self, grant_id: str) -> None:
        with self._lock:
            self._connection.execute("UPDATE approvals SET uses=uses+1 WHERE id=?", (grant_id,))
            self._connection.commit()

    def forget(self, grant_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM approvals WHERE id=?", (grant_id,))
            self._connection.commit()
            return cursor.rowcount > 0

    def list(self, pane: str = "") -> list[Grant]:
        query = "SELECT * FROM approvals WHERE expires>?"
        params: list = [time.time()]
        if pane:
            query += " AND pane=?"
            params.append(pane)
        query += " ORDER BY created DESC"
        with self._lock:
            return [self._grant(row) for row in self._connection.execute(query, params)]

    def purge(self) -> int:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM approvals WHERE expires<=?",
                                              (time.time(),))
            self._connection.commit()
            return cursor.rowcount

    @staticmethod
    def _grant(row: sqlite3.Row) -> Grant:
        return Grant(id=row["id"], pane=row["pane"], signature=row["signature"],
                     action=row["action"], scope=row["scope"], turn=row["turn"],
                     label=row["label"], created=row["created"], expires=row["expires"],
                     uses=row["uses"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()
