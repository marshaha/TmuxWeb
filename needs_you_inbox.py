"""In-app needs-you inbox: the history behind the "needs you" card.

Borrowed from Open WebUI (``backend/open_webui/routers/notifications.py``,
HEAD b0e6098, 0.13.3): an in-app inbox with cursor pagination (``limit=20``),
an unread count (``/inbox/count``), and open-marks-read (``/inbox/open``),
with realtime delivered by socket.io (``socket/main.py``'s ``emit_to_users``
into per-user rooms). TmuxWeb borrows the inbox shape only:

* no socket.io — realtime reuses the existing poll rhythm (the console
  already polls ``/api/panes/status`` every 9s; the inbox badge polls its
  count every 30s). No Web Push either: Open WebUI itself has none
  (``vapid|web-push|PushManager``: zero hits), so there is nothing to copy.
* no multi-user / group model — TmuxWeb is a single-user product, so there
  is no ``user`` / ``visible_clause``: one inbox, one reader.

Why a new table, not the ``mailbox.py`` SQLite: that mailbox is agent-to-agent
messaging (``mcp_server.Mailbox``: fixed sender/recipient identities
claude/codex/hermes, reply threads, ``read_at``). A needs-you record is none
of that — it is keyed by pane, carries a menu title + option labels, and is
resolved when the wait clears, never replied to. Reusing the messages table
would mean stuffing pane ids into ``sender`` and menu text into ``body`` and
losing the pane/title/resolved columns the inbox UI needs. So the inbox is
its own small store next to ``home_store.py``, same shape (one sqlite file,
one connection behind an RLock, WAL).

Retention — 30 days AND at most 200 rows, pruned on every insert:

* 30 days: a wait you did not answer within a month is archaeology, not a
  todo. The card itself is the live signal; the inbox is recent history.
* 200 rows: lightings are rare (a sweep lights a pane at most once per
  distinct menu, and the gate needs two stable sweeps first), so 200 rows is
  months of history at any plausible rate, and it bounds the file.
* Both bounds, oldest first: time first (anything older than 30 days goes,
  even if the table is small), then count (the oldest rows go once the
  table is over 200, even if they are recent). Deleting by age alone would
  let a burst keep the file growing; deleting by count alone would keep a
  stale row forever on a quiet server.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import needs_input

# A lighting is recorded once per distinct menu per pane; see sync_lighting.
MAX_TITLE = 200
MAX_SUMMARY = 400
MAX_OPTIONS = 12
# 一条"正在等"的记录最多挂这么久；超过就当它已经不是活的（菜单没了 / pane 死了）。
# 有它兜底，即使 start-up 的 resolve_open 被跳过，bell 也不会堆成坟场。
OPEN_TTL_HOURS = 12.0
RETENTION_DAYS = 30
MAX_ITEMS = 200
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _now() -> float:
    return time.time()


def menu_summary(prompt: dict | None, max_len: int = MAX_SUMMARY) -> str:
    """One line for the inbox list: title + option labels.

    Only labels survive — the key sequences (``keys``) are derived on the
    console from the live prompt and are never stored.
    """
    if not prompt:
        return ""
    title = str(prompt.get("title") or "").strip()
    options = prompt.get("options") or []
    labels = []
    for option in options[:MAX_OPTIONS]:
        label = str(option.get("label") or "").strip()
        if not label:
            continue
        number = option.get("number")
        labels.append(f"{number}. {label}" if number else label)
    body = str(prompt.get("hint") or "") if prompt.get("source") == "cursor_menu_notice" else " / ".join(labels)
    if len(options) > MAX_OPTIONS:
        body += f" 等 {len(options)} 项"
    summary = " — ".join(part for part in (title, body) if part)
    if not summary:
        summary = str(prompt.get("kind") or "等你回答")
    if len(summary) > max_len:
        summary = summary[: max_len - 1] + "…"
    return summary


def _option_labels(prompt: dict | None) -> list[dict]:
    options = []
    for option in (prompt or {}).get("options") or []:
        label = str(option.get("label") or "").strip()
        meta = str(option.get("meta") or "").strip()
        if not label and not meta:
            continue
        options.append({"label": label, "meta": meta,
                        "number": option.get("number") or ""})
    return options[:MAX_OPTIONS]


class NeedsYouInbox:
    """One row per lighting: a pane lit up, the menu it showed, when."""

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
                """CREATE TABLE IF NOT EXISTS inbox (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       pane_id TEXT NOT NULL,
                       host_id TEXT NOT NULL DEFAULT '',
                       title TEXT NOT NULL DEFAULT '',
                       kind TEXT NOT NULL DEFAULT '',
                       summary TEXT NOT NULL DEFAULT '',
                       options_json TEXT NOT NULL DEFAULT '[]',
                       created_at REAL NOT NULL,
                       read_at REAL,
                       resolved_at REAL,
                       resolved_reason TEXT NOT NULL DEFAULT ''
                   )"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS inbox_created_idx ON inbox(id DESC)")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["options"] = json.loads(row["options_json"] or "[]")
        except (ValueError, TypeError):
            item["options"] = []
        del item["options_json"]
        return item

    def record(self, *, pane_id: str, host_id: str = "", title: str = "",
               kind: str = "", summary: str = "", options: list | None = None) -> int:
        """Insert one lighting; returns its id. Prunes per the retention rule."""
        pane_id = (pane_id or "").strip()
        if not pane_id:
            raise ValueError("pane_id must not be empty")
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO inbox
                       (pane_id, host_id, title, kind, summary, options_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (pane_id, host_id or "", (title or "")[:MAX_TITLE], kind or "",
                 (summary or "")[:MAX_SUMMARY],
                 json.dumps(list(options or [])[:MAX_OPTIONS], ensure_ascii=False),
                 _now()),
            )
            record_id = int(cursor.lastrowid)
            self._conn.commit()
            self.prune()
            return record_id

    def resolve_open(self, reason: str = "restarted") -> int:
        """Close every row still open. Called at startup.

        An open row means "this pane is waiting for an answer *right now*". That
        is a fact about a live sweep, and a sweep does not survive a restart: the
        in-memory lighting map is gone, so nothing will ever come along to clear
        those rows. Left alone they pile up — 135 open rows for 0 waiting panes
        is what made the bell look like it was crying wolf. Whatever is genuinely
        waiting gets recorded again by the next sweep, a second later.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE inbox SET resolved_at = ?, resolved_reason = ? "
                "WHERE resolved_at IS NULL", (_now(), reason))
            self._conn.commit()
            return int(cursor.rowcount)

    def list_items(self, *, limit: int = DEFAULT_LIMIT, before: int | None = None,
                   open_only: bool = False) -> dict:
        """Newest first; ``before`` is the id cursor (Open WebUI's shape, with
        the autoincrement id standing in for its ``created_at:id`` cursor —
        ids only grow, so the order is the same)."""
        if not 1 <= int(limit) <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        clause = "resolved_at IS NULL" if open_only else "1=1"
        with self._lock:
            if before is None:
                rows = self._conn.execute(
                    "SELECT * FROM inbox WHERE " + clause + " ORDER BY id DESC LIMIT ?",
                    (int(limit) + 1,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM inbox WHERE " + clause
                    + " AND id < ? ORDER BY id DESC LIMIT ?",
                    (int(before), int(limit) + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [self._row(row) for row in rows],
                "next_cursor": rows[-1]["id"] if more else None}

    def unread_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE read_at IS NULL").fetchone()
        return int(row[0])

    def open_count(self) -> int:
        """还开着的（真的在等）—— 铃铛上的数字应该是这个。

        以前用的是"未读"，于是历史里没点过的行会一直把铃铛点亮，哪怕菜单早就没了。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE resolved_at IS NULL").fetchone()
        return int(row[0])

    def mark_all_read(self) -> int:
        """Open-marks-read, as in Open WebUI's ``POST /inbox/open``: every
        unread row gets the same ``read_at``. Returns how many were marked."""
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE inbox SET read_at = ? WHERE read_at IS NULL", (now,))
            self._conn.commit()
            return int(cursor.rowcount)

    def mark_read(self, item_id: int) -> dict | None:
        """Mark one row read; None when there is no such row."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE inbox SET read_at = COALESCE(read_at, ?) WHERE id = ?",
                (now, int(item_id)))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM inbox WHERE id = ?", (int(item_id),)).fetchone()
        return self._row(row) if row is not None else None

    def resolve_record(self, record_id: int, reason: str) -> bool:
        """Close a lighting: the wait ended (``cleared``) or a different menu
        took its place on the same pane (``superseded``). The row stays in
        history — resolving only stamps it, never deletes it."""
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE inbox SET resolved_at = COALESCE(resolved_at, ?),
                       resolved_reason = ?
                   WHERE id = ? AND resolved_at IS NULL""",
                (now, reason or "", int(record_id)))
            self._conn.commit()
            return cursor.rowcount > 0

    def prune(self, now: float | None = None) -> int:
        """Enforce the retention rule: older than 30 days, then over 200 rows.
        Oldest first in both cases. Returns rows deleted.

        Also closes rows that have been "open" for longer than OPEN_TTL_HOURS:
        a menu that has been waiting half a day is either gone or the pane is
        dead, and either way the bell should stop pointing at it.
        """
        now = _now() if now is None else now
        with self._lock:
            self._conn.execute(
                "UPDATE inbox SET resolved_at = ?, resolved_reason = 'expired' "
                "WHERE resolved_at IS NULL AND created_at < ?",
                (now, now - OPEN_TTL_HOURS * 3600))
        cutoff = now - RETENTION_DAYS * 86400
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM inbox WHERE created_at < ?", (cutoff,))
            deleted = int(cursor.rowcount)
            count = int(self._conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])
            if count > MAX_ITEMS:
                cursor = self._conn.execute(
                    """DELETE FROM inbox WHERE id IN (
                           SELECT id FROM inbox ORDER BY id ASC LIMIT ?)""",
                    (count - MAX_ITEMS,))
                deleted += int(cursor.rowcount)
            self._conn.commit()
            return deleted


def sync_lighting(store: NeedsYouInbox, lit: dict, states: list[dict]) -> dict:
    """Fold one sweep's states into the inbox; ``lit`` is the caller's
    pane_id -> {"signature": str, "record_id": int} for lightings still open.

    * a pane whose gate just lit (or lit a different menu) gets a new row;
      the previous open row for that pane is resolved as ``superseded``.
    * a pane that was lit and now shows no ``needs_input`` gets its open row
      resolved as ``cleared`` — the row stays in history either way.
    * a pane that vanished from the sweep entirely is left alone: gone from
      the list can mean its host is unreachable, not that it closed, and a
      reappearing pane with the same menu must not record a duplicate.
    """
    for state in states or []:
        pane_id = state.get("id") or ""
        if not pane_id:
            continue
        prompt = state.get("prompt") if state.get("needs_input") else None
        if prompt is None:
            entry = lit.get(pane_id)
            if entry is not None:
                store.resolve_record(entry["record_id"], "cleared")
                del lit[pane_id]
            continue
        signature = needs_input.prompt_signature(prompt)
        if prompt.get("source") == "codex_command_approval" and prompt.get("screen_revision"):
            signature = ("notice", "screen-" + prompt["screen_revision"])
        entry = lit.get(pane_id)
        if entry is not None and entry.get("signature") == signature:
            continue
        if entry is not None:
            store.resolve_record(entry["record_id"], "superseded")
        record_id = store.record(
            pane_id=pane_id, host_id=str(state.get("host_id") or ""),
            title=str(state.get("title") or prompt.get("title") or ""),
            kind=str(prompt.get("kind") or ""),
            summary=menu_summary(prompt), options=_option_labels(prompt))
        lit[pane_id] = {"signature": signature, "record_id": record_id}
    return lit
