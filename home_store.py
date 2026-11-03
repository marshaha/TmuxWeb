"""Favourites and dashboard cards for the Home page.

Small sqlite store with two tables:

* ``favorites`` — one row per starred pane (``pane_id`` is host-scoped, e.g.
  ``localhost~%7`` or ``1d7ef54375eaabc3~%69``), with an optional note.
* ``cards`` — free-form cards an agent (or a person) publishes to the board via
  ``publish_home_card`` / ``POST /api/home/cards``.
* ``pins`` — panes the user nailed to the top of the sidebar's 最近 block. Pins
  survive the recents cap, so a pinned pane stays even after 30 other visits.
* ``pane_numbers`` — the short number every pane is addressed by (`#12`). Numbers
  are assigned once, never reused, and outlive the pane: a stale `#12` must 404
  instead of silently pointing at somebody else's session. That is what makes
  "ask #12" usable in the MCP relay across hosts.

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
MAX_RECENT = 30
MAX_PINS = 20
# 编号不回收：给某个 agent 说过 "#12" 之后，这个号码不该变成别人
MAX_PANE_NUMBERS = 5000


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
                       title TEXT NOT NULL DEFAULT '',
                       created_at REAL NOT NULL
                   )"""
            )
            # older databases predate the custom title
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(favorites)")}
            if "title" not in columns:
                self._conn.execute("ALTER TABLE favorites ADD COLUMN title TEXT NOT NULL DEFAULT ''")
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
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS recent (
                       pane_id TEXT PRIMARY KEY,
                       visited_at REAL NOT NULL
                   )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS pins (
                       pane_id TEXT PRIMARY KEY,
                       label TEXT NOT NULL DEFAULT '',
                       created_at REAL NOT NULL
                   )"""
            )
            # small key/value bag for UI preferences that should follow the account, not the browser
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS prefs (
                       key TEXT PRIMARY KEY,
                       value TEXT NOT NULL DEFAULT ''
                   )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS pane_numbers (
                       pane_id TEXT PRIMARY KEY,
                       number INTEGER NOT NULL UNIQUE,
                       assigned_at REAL NOT NULL
                   )"""
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------- prefs
    # ---------------------------------------------------------------- pane numbers
    def pane_numbers(self) -> dict[str, int]:
        """pane_id → 编号；没有分过号的 pane 不在里面。"""
        with self._lock:
            rows = self._conn.execute("SELECT pane_id, number FROM pane_numbers").fetchall()
        return {row["pane_id"]: int(row["number"]) for row in rows}

    def number_for(self, pane_id: str) -> int:
        """给这个 pane 一个编号，第一次见到时分配；同一 pane 永远同一个号。"""
        pane_id = (pane_id or "").strip()
        if not pane_id:
            raise ValueError("pane id is required")
        with self._lock:
            row = self._conn.execute(
                "SELECT number FROM pane_numbers WHERE pane_id = ?", (pane_id,)
            ).fetchone()
            if row:
                return int(row["number"])
            row = self._conn.execute("SELECT MAX(number) AS top FROM pane_numbers").fetchone()
            number = int(row["top"] or 0) + 1
            self._conn.execute(
                "INSERT INTO pane_numbers (pane_id, number, assigned_at) VALUES (?, ?, ?)",
                (pane_id, number, _now()),
            )
            self._conn.commit()
            # 老数据库不会无限长：超过上限就丢掉最久没被看到的编号（号码仍然不回收）
            self._conn.execute(
                "DELETE FROM pane_numbers WHERE pane_id NOT IN "
                "(SELECT pane_id FROM pane_numbers ORDER BY assigned_at DESC LIMIT ?)",
                (MAX_PANE_NUMBERS,),
            )
            self._conn.commit()
            return number

    def pane_for_number(self, number: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT pane_id FROM pane_numbers WHERE number = ?", (int(number),)
            ).fetchone()
        return row["pane_id"] if row else None

    def ensure_numbers(self, pane_ids) -> dict[str, int]:
        """一次给一批 pane 分号（侧栏每次刷新都会调，已分过的直接返回）。"""
        wanted = [str(pane).strip() for pane in pane_ids if str(pane or "").strip()]
        if not wanted:
            return {}
        with self._lock:
            existing = self.pane_numbers()
            missing = [pane for pane in wanted if pane not in existing]
            for pane in missing:
                existing[pane] = self.number_for(pane)
        return {pane: existing[pane] for pane in wanted}

    def get_prefs(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM prefs").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_pref(self, key: str, value: str) -> dict[str, str]:
        key = (key or "").strip()[:64]
        if not key:
            raise ValueError("pref key is required")
        with self._lock:
            self._conn.execute(
                "INSERT INTO prefs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, (value or "").strip()[:200]),
            )
            self._conn.commit()
        return self.get_prefs()

    # ---------------------------------------------------------------- favorites
    def add_favorite(self, pane_id: str, note: str = "", title: str = "") -> dict:
        pane_id = pane_id.strip()
        if not pane_id:
            raise ValueError("pane_id is required")
        with self._lock:
            self._conn.execute(
                """INSERT INTO favorites (pane_id, note, title, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(pane_id) DO UPDATE SET note = excluded.note,
                       title = CASE WHEN excluded.title = '' THEN favorites.title ELSE excluded.title END""",
                (pane_id, note[:2000], title[:MAX_TITLE], _now()),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM favorites WHERE pane_id = ?", (pane_id,)).fetchone()
        return dict(row)

    def set_favorite_title(self, pane_id: str, title: str) -> dict:
        """Rename a card. An empty title falls back to the generated default.

        A pane has exactly one name: if it is also pinned, the pin's label follows,
        so the sidebar and the board never disagree.
        """
        pane_id = pane_id.strip()
        clean = (title or "").strip()[:MAX_TITLE]
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE favorites SET title = ? WHERE pane_id = ?", (clean, pane_id)
            )
            self._conn.execute("UPDATE pins SET label = ? WHERE pane_id = ?", (clean, pane_id))
            self._conn.commit()
            if not cursor.rowcount:
                raise KeyError(pane_id)
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

    # ---------------------------------------------------------------- recent visits
    def touch_recent(self, pane_id: str) -> None:
        """Record that a pane was opened; keeps a bounded, newest-first list."""
        pane_id = (pane_id or "").strip()
        if not pane_id:
            raise ValueError("pane_id is required")
        with self._lock:
            self._conn.execute(
                """INSERT INTO recent (pane_id, visited_at) VALUES (?, ?)
                   ON CONFLICT(pane_id) DO UPDATE SET visited_at = excluded.visited_at""",
                (pane_id, _now()),
            )
            self._conn.execute(
                "DELETE FROM recent WHERE pane_id NOT IN"
                " (SELECT pane_id FROM recent ORDER BY visited_at DESC LIMIT ?)",
                (MAX_RECENT,),
            )
            self._conn.commit()

    def recent(self, limit: int = 4) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recent ORDER BY visited_at DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------- pins
    def pin(self, pane_id: str, label: str = "") -> dict:
        """Nail a pane to the top of 最近. Pinning twice just refreshes the label.

        Pinning deliberately ignores the board: a favourite and a pin are separate
        lists, so pinning a pane that happens to be starred does not copy the card's
        title over. Only an explicit rename links the two names (see rename_pin).
        """
        pane_id = (pane_id or "").strip()
        if not pane_id:
            raise ValueError("pane_id is required")
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO pins (pane_id, label, created_at) VALUES (?, ?, ?)
                   ON CONFLICT(pane_id) DO UPDATE SET
                       label = CASE WHEN excluded.label = '' THEN pins.label ELSE excluded.label END""",
                (pane_id, (label or "").strip()[:MAX_TITLE], now),
            )
            self._conn.execute(
                "DELETE FROM pins WHERE pane_id NOT IN"
                " (SELECT pane_id FROM pins ORDER BY created_at DESC LIMIT ?)",
                (MAX_PINS,),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM pins WHERE pane_id = ?", (pane_id,)).fetchone()
        return dict(row)

    def rename_pin(self, pane_id: str, label: str) -> dict:
        """Give a pinned pane its own name; an empty label goes back to the default.

        If the same pane also sits on the board, the card's title is updated in the
        same transaction -- the user asked for one name, not two.
        """
        pane_id = (pane_id or "").strip()
        if not pane_id:
            raise ValueError("pane_id is required")
        clean = (label or "").strip()[:MAX_TITLE]
        with self._lock:
            cursor = self._conn.execute("UPDATE pins SET label = ? WHERE pane_id = ?", (clean, pane_id))
            if not cursor.rowcount:
                raise KeyError(pane_id)
            self._conn.execute("UPDATE favorites SET title = ? WHERE pane_id = ?", (clean, pane_id))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM pins WHERE pane_id = ?", (pane_id,)).fetchone()
        return dict(row)

    def unpin(self, pane_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM pins WHERE pane_id = ?", ((pane_id or "").strip(),))
            self._conn.commit()
            return cursor.rowcount > 0

    def pins(self) -> list[dict]:
        """Newest pin first: the thing you just nailed to the top is the thing you want to see."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM pins ORDER BY created_at DESC, pane_id ASC").fetchall()
        return [dict(row) for row in rows]

    def is_pinned(self, pane_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM pins WHERE pane_id = ?", ((pane_id or "").strip(),)).fetchone()
        return row is not None

    def forget_recent(self, pane_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM recent WHERE pane_id = ?", ((pane_id or "").strip(),))
            self._conn.commit()
            return cursor.rowcount > 0

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
