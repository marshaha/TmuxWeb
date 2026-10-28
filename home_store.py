"""Favourites and dashboard cards for the Home page.

Small sqlite store with two tables:

* ``favorites`` — one row per starred pane (``pane_id`` is host-scoped, e.g.
  ``localhost~%7`` or ``1d7ef54375eaabc3~%69``), with an optional note.
* ``cards`` — free-form cards an agent (or a person) publishes to the board via
  ``publish_home_card`` / ``POST /api/home/cards``.

The store is deliberately independent of tmux/shares so it can be unit tested
without a server.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

CARD_KINDS = ("note", "result", "alert", "link")
MAX_TITLE = 200
MAX_BODY = 20000
MAX_CARDS = 200


def _now() -> float:
    return time.time()


class HomeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS favorites (
                       pane_id TEXT PRIMARY KEY,
                       note TEXT NOT NULL DEFAULT '',
                       created_at REAL NOT NULL
                   )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS cards (
                       id TEXT PRIMARY KEY,
                       title TEXT NOT NULL,
                       body TEXT NOT NULL DEFAULT '',
                       pane_id TEXT NOT NULL DEFAULT '',
                       kind TEXT NOT NULL DEFAULT 'note',
                       created_at REAL NOT NULL,
                       updated_at REAL NOT NULL
                   )"""
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- favorites
    def add_favorite(self, pane_id: str, note: str = "") -> dict:
        pane_id = pane_id.strip()
        if not pane_id:
            raise ValueError("pane_id is required")
        with self._lock:
            self._conn.execute(
                """INSERT INTO favorites (pane_id, note, created_at) VALUES (?, ?, ?)
                   ON CONFLICT(pane_id) DO UPDATE SET note = excluded.note""",
                (pane_id, note[:2000], _now()),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM favorites WHERE pane_id = ?", (pane_id,)).fetchone()
        return dict(row)

    def remove_favorite(self, pane_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM favorites WHERE pane_id = ?", (pane_id.strip(),))
            self._conn.commit()
            return cursor.rowcount > 0

    def favorites(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM favorites ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def is_favorite(self, pane_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM favorites WHERE pane_id = ?", (pane_id.strip(),)).fetchone()
        return row is not None

    # ---------------------------------------------------------------- cards
    def add_card(self, title: str, body: str = "", pane_id: str = "", kind: str = "note") -> dict:
        title = (title or "").strip()[:MAX_TITLE]
        if not title:
            raise ValueError("title is required")
        kind = kind if kind in CARD_KINDS else "note"
        now = _now()
        card = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "body": (body or "")[:MAX_BODY],
            "pane_id": (pane_id or "").strip(),
            "kind": kind,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO cards (id, title, body, pane_id, kind, created_at, updated_at)"
                " VALUES (:id, :title, :body, :pane_id, :kind, :created_at, :updated_at)",
                card,
            )
            # keep the board bounded: drop the oldest beyond MAX_CARDS
            self._conn.execute(
                "DELETE FROM cards WHERE id NOT IN (SELECT id FROM cards ORDER BY created_at DESC LIMIT ?)",
                (MAX_CARDS,),
            )
            self._conn.commit()
        return card

    def card(self, card_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return dict(row) if row else None

    def cards(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_card(self, card_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            self._conn.commit()
            return cursor.rowcount > 0
