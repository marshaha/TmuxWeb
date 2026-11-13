"""单向联邦：把另一台 TmuxWeb 当"一台主机"接进来（第一阶段只读）。

两半在同一模块里：

* **被集成端**（比如 oracle 上的 ddr）用 :class:`FedGrants` 签发**联邦令牌**：
  只需要 ``view`` 就能看屏幕/对话，``input``/``files``/``admin`` 另外给。可见范围
  （哪些 pane）也记在令牌上，由被集成端裁剪 —— 客户端自律不可靠。
* **集成端**（robo）用 :class:`FederationStore` 记住节点，用 :class:`FedClient`
  调对方的 ``/api/fed/*``，再包一层 :class:`FedTransport`，让它长得跟
  ``RemoteHostTransport`` 一样：pane 列表、capture、transcript 都能直接用。

节点 id 故意用 16 位十六进制，和 SSH 主机同一个形状，于是 ``<id>~%12`` 这套
pane 引用、编号、URL 编码全都不用改；显示时再加 ``ddr#12`` 的前缀。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_PREFIX = "fed_"

# Cloudflare 的"浏览器完整性检查"会直接封掉 python 默认的 User-Agent（Error 1010），
# 所以每个联邦请求都带一个说清自己是谁的 UA —— 实测它能过，python-urllib 不能。
USER_AGENT = "TmuxWeb-Federation/1.0 (+https://github.com/marshaha/TmuxWeb)"
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
SCOPES = ("view", "input", "files", "admin")
DEFAULT_SCOPES = ("view",)
MAX_TTL = 60 * 60 * 24 * 30


class FederationError(Exception):
    """Anything the caller should surface as a 4xx/503 rather than a traceback."""


def node_id() -> str:
    return secrets.token_hex(8)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


# ---------------------------------------------------------------- 被集成端（发令牌）


class FedGrants:
    """Issued tokens for other TmuxWeb servers. Store the hash, never the token."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.connection = _connect(path)
        with self.lock:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS fed_tokens("
                " id TEXT PRIMARY KEY, label TEXT, secret_hash TEXT, scopes TEXT,"
                " panes TEXT, created_at REAL, expires_at REAL, revoked_at REAL)"
            )
            self.connection.commit()

    def issue(self, label: str, scopes: list[str], panes, ttl: int) -> dict:
        granted = [scope for scope in scopes if scope in SCOPES]
        if not granted:
            raise FederationError("scopes must contain at least one of " + ", ".join(SCOPES))
        if "view" not in granted:
            raise FederationError("view is required: without it the node has nothing to read")
        ttl = int(ttl or 0)
        # ttl=0 就是永久：不发到期时间，只能靠吊销收回（用户要的"可以永久"）
        expires_at = None if ttl <= 0 else time.time() + max(300, min(ttl, MAX_TTL))
        token_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(24)
        token = f"{TOKEN_PREFIX}{token_id}.{secret}"
        now = time.time()
        with self.lock:
            self.connection.execute(
                "INSERT INTO fed_tokens(id,label,secret_hash,scopes,panes,created_at,expires_at,revoked_at)"
                " VALUES (?,?,?,?,?,?,?,NULL)",
                (token_id, (label or "")[:64], hash_secret(secret), json.dumps(granted),
                 json.dumps(panes if panes is not None else "all"), now, expires_at),
            )
            self.connection.commit()
        return {"id": token_id, "token": token, "scopes": granted, "panes": panes,
                "label": label, "expires_at": expires_at}

    def verify(self, token: str) -> dict:
        """token → grants; raises FederationError when unknown, revoked or expired."""
        text = (token or "").strip()
        if not text.startswith(TOKEN_PREFIX) or "." not in text:
            raise FederationError("not a federation token")
        token_id, _, secret = text[len(TOKEN_PREFIX):].partition(".")
        with self.lock:
            row = self.connection.execute("SELECT * FROM fed_tokens WHERE id=?", (token_id,)).fetchone()
        if row is None or not hmac.compare_digest(row["secret_hash"], hash_secret(secret)):
            raise FederationError("unknown federation token")
        if row["revoked_at"] is not None:
            raise FederationError("federation token revoked")
        if row["expires_at"] is not None and row["expires_at"] <= time.time():
            raise FederationError("federation token expired")
        return {"id": row["id"], "label": row["label"] or "",
                "scopes": json.loads(row["scopes"] or "[]"),
                "panes": json.loads(row["panes"] or '"all"'),
                "expires_at": row["expires_at"]}

    def listing(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT id,label,scopes,panes,created_at,expires_at,revoked_at FROM fed_tokens"
                " ORDER BY created_at DESC").fetchall()
        return [{"id": row["id"], "label": row["label"] or "",
                 "scopes": json.loads(row["scopes"] or "[]"),
                 "panes": json.loads(row["panes"] or '"all"'),
                 "created_at": row["created_at"], "expires_at": row["expires_at"],
                 "revoked": row["revoked_at"] is not None} for row in rows]

    def revoke(self, token_id: str) -> bool:
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE fed_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (time.time(), token_id))
            self.connection.commit()
        return cursor.rowcount > 0


def parse_auth_key(text: str) -> tuple[str, str]:
    """`fed_x.y@https://host` → (token, url)。

    界面上只需要粘一串 auth key：子节点生成时把"它被你怎么访问到的地址"接在 `@` 后面，
    父节点就不用再问地址。只写令牌（不带 @）也照收，地址另外填。
    """
    raw = (text or "").strip()
    if "@" in raw:
        token, _, url = raw.rpartition("@")
        url = url.strip().rstrip("/")
        if url.startswith(("http://", "https://")):
            return token.strip(), url
    return raw, ""


def pane_allowed(grants: dict, pane_id: str) -> bool:
    """Is this pane inside the range the token owner shared?"""
    allowed = grants.get("panes", "all")
    if allowed == "all" or allowed is None:
        return True
    if isinstance(allowed, dict) and allowed.get("favorite"):
        return bool((allowed.get("pane_ids") or {}).get(pane_id))
    return pane_id in (allowed or [])


# ---------------------------------------------------------------- 集成端（拉别人的 pane）


class FederationStore:
    """Nodes this console pulls from. Nothing here is exposed to the network."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.connection = _connect(path)
        with self.lock:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS fed_nodes("
                " id TEXT PRIMARY KEY, label TEXT, prefix TEXT UNIQUE, base_url TEXT,"
                " fallback_url TEXT, token TEXT, enabled INTEGER, created_at REAL)"
            )
            self.connection.commit()

    def listing(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT id,label,prefix,base_url,fallback_url,enabled,created_at"
                " FROM fed_nodes ORDER BY created_at").fetchall()
        return [dict(row) | {"enabled": bool(row["enabled"])} for row in rows]

    def get_node(self, host_id: str, include_token: bool = False) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT * FROM fed_nodes WHERE id=?", (host_id,)).fetchone()
        if row is None:
            raise KeyError(host_id)
        node = dict(row)
        node["enabled"] = bool(node["enabled"])
        if not include_token:
            node.pop("token", None)
        return node

    def by_prefix(self, prefix: str) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM fed_nodes WHERE prefix=?", ((prefix or "").lower(),)).fetchone()
        if row is None:
            return None
        node = dict(row)
        node["enabled"] = bool(node["enabled"])
        return node

    def add(self, label: str, prefix: str, base_url: str, fallback_url: str, token: str) -> dict:
        # 界面上只填"名字 + auth key"，前缀就从名字里取（oracle → oracle）
        prefix = re.sub(r"[^a-z0-9]+", "", (prefix or label or "").strip().lower())[:16]
        if not PREFIX_RE.fullmatch(prefix):
            raise FederationError("名字要能当编号前缀用：2-16 位小写字母/数字，以字母开头（比如 oracle）")
        base = (base_url or "").strip().rstrip("/")
        fallback = (fallback_url or "").strip().rstrip("/")
        if not base and not fallback:
            raise FederationError("base_url or fallback_url is required")
        salt = secrets.token_hex(8)
        record = {"id": salt, "label": (label or prefix)[:64], "prefix": prefix,
                  "base_url": base, "fallback_url": fallback,
                  "token": (token or "").strip(), "enabled": 1, "created_at": time.time()}
        try:
            with self.lock:
                self.connection.execute(
                    "INSERT INTO fed_nodes(id,label,prefix,base_url,fallback_url,token,enabled,created_at)"
                    " VALUES (:id,:label,:prefix,:base_url,:fallback_url,:token,:enabled,:created_at)",
                    record)
                self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise FederationError(f"prefix already used: {prefix}") from exc
        record.pop("token")
        return record | {"enabled": True}

    def remove(self, host_id: str) -> bool:
        with self.lock:
            cursor = self.connection.execute("DELETE FROM fed_nodes WHERE id=?", (host_id,))
            self.connection.commit()
        return cursor.rowcount > 0


class FedClient:
    """HTTP client for one node: primary URL first, fall back to the public one."""

    def __init__(self, base_url: str, fallback_url: str, token: str, timeout: float = 8.0):
        self.base_url = (base_url or "").rstrip("/")
        self.fallback_url = (fallback_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def _urls(self) -> list[str]:
        return [url for url in (self.base_url, self.fallback_url) if url]

    def _get(self, path: str, timeout: float | None = None) -> dict:
        errors = []
        for base in self._urls():
            request = urllib.request.Request(base + path, method="GET")
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("Accept", "application/json")
            request.add_header("User-Agent", USER_AGENT)
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    return json.loads(response.read().decode("utf-8", "replace") or "{}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                if "1010" in detail:
                    detail += "（Cloudflare 把这类请求当成机器人封了：换成 tailnet/局域网地址直连，或让对面放行）"
                errors.append(f"{base}: HTTP {exc.code} {detail}")
                if exc.code in (401, 403):
                    break            # 令牌不对，换地址也白搭
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                errors.append(f"{base}: {exc}")
        raise FederationError("; ".join(errors) or "node has no address")

    def ping(self) -> dict:
        return self._get("/api/fed/ping", timeout=3)

    def panes(self, timeout: float = 25.0) -> list[dict]:
        # 对端列 pane 时可能还在等它自己的 SSH 主机，给宽一点；调用方另有缓存
        return self._get("/api/fed/panes", timeout=timeout).get("panes") or []

    def capture(self, pane_id: str, history: int = 80) -> str:
        quoted = urllib.parse.quote(pane_id, safe="")
        return self._get(f"/api/fed/panes/{quoted}/capture?history={int(history)}").get("content") or ""

    def transcript(self, pane_id: str, limit: int = 200) -> dict:
        quoted = urllib.parse.quote(pane_id, safe="")
        return self._get(f"/api/fed/panes/{quoted}/transcript?limit={int(limit)}")


class FedTransport:
    """Looks like a host transport, so panes/capture/transcript need no special cases."""

    read_only = True

    def __init__(self, node: dict, client: FedClient | None = None):
        self.node = node
        self.prefix = str(node.get("prefix") or "")
        self.host_id = str(node.get("id") or "")
        self.client = client or FedClient(node.get("base_url") or "", node.get("fallback_url") or "",
                                          node.get("token") or "")
        self.capture_timeout = 8
        self._panes_cache: tuple[float, list[dict]] | None = None

    # -- panes -------------------------------------------------------------
    def list_panes(self) -> list[dict]:
        now = time.time()
        if self._panes_cache and now - self._panes_cache[0] < 5:
            return [dict(pane) for pane in self._panes_cache[1]]
        found = []
        for pane in self.client.panes():
            entry = dict(pane)
            entry["host_id"] = self.host_id
            entry["prefix"] = self.prefix
            # 上游的编号显示成 `ddr#12`；id 仍然是本地的 `<node>~%12`
            raw_pane = str(entry.get("id") or entry.get("pane_id") or "")
            if not raw_pane.startswith("%"):
                continue
            entry["pane_id"] = raw_pane
            entry["id"] = f"{self.host_id}~{raw_pane}"
            found.append(entry)
        self._panes_cache = (now, found)
        return [dict(pane) for pane in found]

    def validate_pane(self, pane_id: str) -> dict:
        for pane in self.list_panes():
            if pane.get("pane_id") == pane_id:
                return pane
        raise RuntimeError(f"remote pane not found: {pane_id}")

    # -- screen ------------------------------------------------------------
    def capture(self, pane_id: str, history: int = 500, pane: dict | None = None,
                visible_only: bool = False) -> str:
        return self.client.capture(pane_id, history=history)

    def capture_ansi(self, pane_id: str, history: int = 500, pane: dict | None = None,
                     visible_only: bool = False) -> str:
        return self.capture(pane_id, history=history, pane=pane)

    def capture_and_status_many(self, pane_ids, history: int = 40, status_ids=None,
                                panes=None) -> tuple[dict, dict]:
        """sweep 期望 (texts, states)；联邦侧没有状态扫描，返回空的状态表。"""
        return ({pane_id: self.capture(pane_id, history) for pane_id in pane_ids}, {})

    def capture_many(self, pane_ids, history: int = 40, panes=None) -> dict:
        return {pane_id: self.capture(pane_id, history) for pane_id in pane_ids}

    def transcript(self, pane_id: str, limit: int = 200, refresh: bool = False,
                   pane: dict | None = None) -> dict:
        payload = self.client.transcript(pane_id, limit=limit)
        payload.setdefault("pane", pane_id)
        return payload

    def probe_agents(self) -> list[dict]:
        return []

    # -- 写操作：第一阶段明确拒绝，而不是悄悄失败 -------------------------
    def _read_only(self, *_args, **_kwargs):
        raise FederationError("这个节点是只读接进来的（第一阶段的联邦只开放 view）")

    send = _read_only
    send_key = _read_only
    send_keys = _read_only
    kill_pane = _read_only
    kill_session = _read_only


def client_for(node: dict) -> FedClient:
    return FedClient(node.get("base_url") or "", node.get("fallback_url") or "",
                     node.get("token") or "")
