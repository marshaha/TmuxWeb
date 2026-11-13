"""Short-lived tickets for transfers that skip Cloudflare.

The console page normally comes from the public (Cloudflare) origin, but a user
on the same LAN or tailnet can move the *bytes* over a direct origin.  A direct
origin cannot read the console's session cookie (different host), so the page
asks for a ticket instead:

    POST /api/transfer/ticket   (cookie auth)  ->  {"token": ..., "expires": ...}
    GET  /t/download?t=<token>                  (ticket auth, no cookie)
    PUT  /t/upload?t=<token>                    (ticket auth, no cookie)

A ticket is HMAC-SHA256 over a compact JSON payload and carries everything the
data route needs to refuse work it was not minted for: pane, path, direction,
size ceiling and an expiry.  Nothing else in the app accepts a ticket, so a
leaked URL can only move that one file, until it expires.

Replay accounting is deliberately weak: a download is retried and range-read by
browsers, and an upload needs one request per 4 MB chunk, so a ticket allows a
bounded number of uses inside its lifetime instead of exactly one.  That is
still enough to stop a leaked link from being farmed - the ledger is pruned on
every mint and capped, so it cannot be used to grow memory.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

DEFAULT_TTL = 600                  # 10 分钟：足够传完，短到泄露了也很快失效
DOWNLOAD_USES = 8                  # 浏览器会重试、会 Range
UPLOAD_USES_PER_CHUNK = 2          # 每块一次，再留一次重试

_DIRECTIONS = {"up", "down", "zip"}


class TicketError(Exception):
    """A ticket is missing, malformed, expired, spent, or for something else."""


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(text: str) -> dict[str, Any]:
    padding = "=" * (-len(text) % 4)
    return json.loads(base64.urlsafe_b64decode(text + padding))


def _sign(secret: bytes, body: str) -> str:
    return hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:32]


def mint(secret: bytes, *, pane: str, path: str, direction: str,
         max_bytes: int = 0, name: str = "", ttl: int = DEFAULT_TTL,
         uses: int = 0, paths: list[str] | None = None) -> dict[str, Any]:
    """Mint a ticket for one pane path (or a zip of several); returns token + expiry."""
    if direction not in _DIRECTIONS:
        raise ValueError(f"unknown direction: {direction}")
    if not pane or not path:
        raise ValueError("pane and path are required")
    now = int(time.time())
    if uses is None or uses == 0:
        uses = DOWNLOAD_USES if direction == "down" else UPLOAD_USES_PER_CHUNK
    # uses < 0 表示不限次数：上传按块计数天生不可估（块大小会随链路自适应，
    # 256KB~4MB 差 16 倍），数错了用户就会看到"上传失败：ticket already used"。
    # 票据本身已经绑定 pane/路径/大小上限且 10 分钟过期，次数不是防护点。
    payload = {
        "p": pane,
        "f": path,
        "n": name,
        "d": direction,
        "m": int(max_bytes),
        "e": now + int(ttl),
        "u": int(uses),
        "i": secrets.token_urlsafe(9),
    }
    if direction == "zip":
        # 打包下载：pane + 基准目录(f) + 选中列表(z)；一次一张（zip 会重复下载就重复生成）
        wanted = [str(item) for item in (paths or []) if str(item).strip()]
        if not wanted:
            raise ValueError("paths is required for a zip ticket")
        payload["z"] = wanted[:1000]
    body = _encode(payload)
    return {"token": f"{body}.{_sign(secret, body)}", "expires": payload["e"],
            "direction": direction, "pane": pane, "path": path}


def peek(secret: bytes, token: str) -> dict[str, Any]:
    """Verify the signature and expiry only - no use is consumed."""
    if not token or token.count(".") != 1:
        raise TicketError("malformed ticket")
    body, signature = token.split(".", 1)
    if not hmac.compare_digest(_sign(secret, body), signature):
        raise TicketError("bad signature")
    try:
        payload = _decode(body)
    except (ValueError, TypeError) as exc:
        raise TicketError("malformed ticket") from exc
    if not isinstance(payload, dict):
        raise TicketError("malformed ticket")
    if int(payload.get("e", 0)) < int(time.time()):
        raise TicketError("ticket expired")
    if payload.get("d") not in _DIRECTIONS:
        raise TicketError("unknown direction")
    return payload


class Ledger:
    """Bounded counter of how many times each ticket has been used."""

    def __init__(self, limit: int = 4096) -> None:
        self._seen: dict[str, list[int]] = {}      # nonce -> [remaining, expires]
        self._limit = limit

    def _prune(self, now: int) -> None:
        stale = [nonce for nonce, (_left, expires) in self._seen.items() if expires < now]
        for nonce in stale:
            self._seen.pop(nonce, None)
        while len(self._seen) > self._limit:       # 极端情况下也不会无限长
            self._seen.pop(next(iter(self._seen)))

    def spent(self, payload: dict[str, Any]) -> None:
        """Count one use; raises TicketError when the ticket is exhausted."""
        now = int(time.time())
        self._prune(now)
        nonce = str(payload.get("i", ""))
        if not nonce:
            raise TicketError("malformed ticket")
        allowed = int(payload.get("u", 0))
        if allowed < 0:
            return                       # 不计数（上传方向）
        entry = self._seen.get(nonce)
        if entry is None:
            entry = [allowed - 1, int(payload.get("e", 0))]
            self._seen[nonce] = entry
        else:
            entry[0] -= 1
        if entry[0] < 0:
            raise TicketError("ticket already used")

    def reset(self) -> None:
        self._seen.clear()
