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

import asyncio
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

# 打包下载的硬上限（子节点执行，父节点只转发）
ZIP_MAX_BYTES = 128 * 1024 * 1024
ZIP_MAX_FILES = 1000


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

    def update(self, token_id: str, scopes=None, panes=None, ttl=None, label=None) -> dict:
        """改一把已经签出去的 key：权限 / 范围 / 到期（ttl=0 永久，None 表示保持不变）。"""
        with self.lock:
            row = self.connection.execute("SELECT * FROM fed_tokens WHERE id=?", (token_id,)).fetchone()
            if row is None:
                raise KeyError(token_id)
            granted = json.loads(row["scopes"] or "[]") if scopes is None else \
                [scope for scope in scopes if scope in SCOPES]
            if "view" not in granted:
                raise FederationError("view is required: without it the node has nothing to read")
            new_panes = json.loads(row["panes"] or '"all"') if panes is None else panes
            if ttl is None:
                expires = row["expires_at"]
            else:
                ttl = int(ttl)
                expires = None if ttl <= 0 else time.time() + max(300, min(ttl, MAX_TTL))
            new_label = (row["label"] or "") if label is None else (label or "")[:64]
            self.connection.execute(
                "UPDATE fed_tokens SET scopes=?,panes=?,expires_at=?,label=? WHERE id=?",
                (json.dumps(granted), json.dumps(new_panes), expires, new_label, token_id))
            self.connection.commit()
        return {"id": token_id, "label": new_label, "scopes": granted, "panes": new_panes,
                "expires_at": expires}

    def purge(self, token_id: str) -> bool:
        """删掉这条记录（吊销只标记，这条才是从列表里清掉）。"""
        with self.lock:
            cursor = self.connection.execute("DELETE FROM fed_tokens WHERE id=?", (token_id,))
            self.connection.commit()
        return cursor.rowcount > 0

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


class FedPolicy:
    """子节点自己管的开关：允许外部做哪些操作（和具体某把 key 的作用域无关）。

    令牌里的 scopes 是"这把钥匙想干什么"，这里是"这台机器允不允许这类事"。两边都通过才执行，
    所以管理员关掉「发按键」之后，已经签出去的钥匙也立刻失效。
    """

    FLAGS = {"input": "允许外部发消息/按键", "files": "允许外部读写文件",
             "admin": "允许外部管理会话（建/杀窗格、开终端）"}

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.connection = _connect(path)
        with self.lock:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS fed_policy(name TEXT PRIMARY KEY, value INTEGER)")
            self.connection.commit()

    def read(self) -> dict:
        with self.lock:
            rows = self.connection.execute("SELECT name,value FROM fed_policy").fetchall()
        stored = {row["name"]: bool(row["value"]) for row in rows}
        # 默认：只开 view。写操作要管理员显式打开
        return {name: stored.get(name, False) for name in self.FLAGS}

    def allows(self, scope: str) -> bool:
        return scope == "view" or bool(self.read().get(scope, False))

    def write(self, flags: dict) -> dict:
        with self.lock:
            for name in self.FLAGS:
                if name not in flags:
                    continue
                self.connection.execute(
                    "INSERT INTO fed_policy(name,value) VALUES (?,?)"
                    " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (name, 1 if flags[name] else 0))
            self.connection.commit()
        return self.read()


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
                " fallback_url TEXT, token TEXT, enabled INTEGER, created_at REAL,"
                " prefer TEXT DEFAULT 'direct')"
            )
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(fed_nodes)")}
            if "prefer" not in columns:                 # 老库补列：直连优先，和之前的行为一致
                self.connection.execute("ALTER TABLE fed_nodes ADD COLUMN prefer TEXT DEFAULT 'direct'")
            self.connection.commit()

    def listing(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT id,label,prefix,base_url,fallback_url,enabled,created_at,prefer"
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

    def add(self, label: str, prefix: str, base_url: str, fallback_url: str, token: str,
            prefer: str = "direct") -> dict:
        # 界面上只填"名字 + auth key"，前缀就从名字里取（oracle → oracle）
        prefix = re.sub(r"[^a-z0-9]+", "", (prefix or label or "").strip().lower())[:16]
        if not PREFIX_RE.fullmatch(prefix):
            raise FederationError("名字要能当编号前缀用：2-16 位小写字母/数字，以字母开头（比如 oracle）")
        base = (base_url or "").strip().rstrip("/")
        fallback = (fallback_url or "").strip().rstrip("/")
        if not base and not fallback:
            raise FederationError("base_url or fallback_url is required")
        salt = secrets.token_hex(8)
        prefer = "public" if prefer == "public" else "direct"
        if prefer == "public" and fallback and not base:
            base, fallback = fallback, base          # 只有一条地址时不影响
        record = {"id": salt, "label": (label or prefix)[:64], "prefix": prefix,
                  "base_url": base, "fallback_url": fallback,
                  "token": (token or "").strip(), "enabled": 1, "created_at": time.time(),
                  "prefer": prefer}
        try:
            with self.lock:
                self.connection.execute(
                    "INSERT INTO fed_nodes(id,label,prefix,base_url,fallback_url,token,enabled,"
                    "created_at,prefer)"
                    " VALUES (:id,:label,:prefix,:base_url,:fallback_url,:token,:enabled,"
                    ":created_at,:prefer)", record)
                self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise FederationError(f"prefix already used: {prefix}") from exc
        record.pop("token")
        return record | {"enabled": True}

    def set_prefer(self, host_id: str, prefer: str) -> dict:
        """`direct` / `public`：把首选线路换到 base_url 上（FedClient 永远先试它）。"""
        prefer = "public" if prefer == "public" else "direct"
        with self.lock:
            row = self.connection.execute("SELECT * FROM fed_nodes WHERE id=?", (host_id,)).fetchone()
            if row is None:
                raise KeyError(host_id)
            current = (row["prefer"] or "direct")
            base, fallback = row["base_url"] or "", row["fallback_url"] or ""
            if prefer != current and base and fallback:
                base, fallback = fallback, base
            self.connection.execute(
                "UPDATE fed_nodes SET base_url=?, fallback_url=?, prefer=? WHERE id=?",
                (base, fallback, prefer, host_id))
            self.connection.commit()
        with self.lock:
            row = self.connection.execute("SELECT * FROM fed_nodes WHERE id=?", (host_id,)).fetchone()
        node = dict(row)
        node["enabled"] = bool(node["enabled"])
        node.pop("token", None)
        return node

    def set_addresses(self, host_id: str, base_url: str, fallback_url: str) -> dict:
        """记下发回来的地址（父节点从 ping 里学到直连地址时用）。"""
        with self.lock:
            row = self.connection.execute("SELECT * FROM fed_nodes WHERE id=?", (host_id,)).fetchone()
            if row is None:
                raise KeyError(host_id)
            self.connection.execute(
                "UPDATE fed_nodes SET base_url=?, fallback_url=? WHERE id=?",
                ((base_url or "").rstrip("/"), (fallback_url or "").rstrip("/"), host_id))
            self.connection.commit()
            row = self.connection.execute("SELECT * FROM fed_nodes WHERE id=?", (host_id,)).fetchone()
        node = dict(row)
        node["enabled"] = bool(node["enabled"])
        node.pop("token", None)
        return node

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

    def _call(self, method: str, path: str, data: bytes | None = None,
              headers: dict | None = None, timeout: float | None = None, raw: bool = False):
        errors = []
        for base in self._urls():
            request = urllib.request.Request(base + path, method=method, data=data)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("User-Agent", USER_AGENT)
            for name, value in (headers or {}).items():
                request.add_header(name, value)
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    payload = response.read()
                    if raw:
                        return payload
                    return json.loads(payload.decode("utf-8", "replace") or "{}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                if "1010" in detail:
                    detail += "（Cloudflare 把这类请求当成机器人封了：换成 tailnet/局域网地址直连，或让对面放行）"
                errors.append(f"{base}: HTTP {exc.code} {detail}")
                if exc.code in (401, 403):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                errors.append(f"{base}: {exc}")
        raise FederationError("; ".join(errors) or "node has no address")

    def input(self, pane_id: str, text: str, submit: bool = True) -> dict:
        body = json.dumps({"text": text, "submit": bool(submit)}).encode()
        return self._call("POST", f"/api/fed/panes/{urllib.parse.quote(pane_id, safe='')}/input",
                          body, {"Content-Type": "application/json"})

    def key(self, pane_id: str, key: str) -> dict:
        body = json.dumps({"key": key}).encode()
        return self._call("POST", f"/api/fed/panes/{urllib.parse.quote(pane_id, safe='')}/key",
                          body, {"Content-Type": "application/json"})

    def files(self, path: str = "", hidden: bool = False, limit: int = 2000) -> dict:
        query = urllib.parse.urlencode({"path": path, "hidden": int(bool(hidden)), "limit": int(limit)})
        return self._call("GET", f"/api/fed/files?{query}", timeout=20)

    def stat(self, path: str) -> dict:
        return self._call("GET", "/api/fed/files/stat?" + urllib.parse.urlencode({"path": path}),
                          timeout=20)

    def download(self, path: str) -> bytes:
        return self._call("GET", "/api/fed/files/download?" + urllib.parse.urlencode({"path": path}),
                          timeout=60, raw=True)

    def upload(self, directory: str, name: str, data: bytes, offset: int = 0, total: int = 0) -> dict:
        query = urllib.parse.urlencode({"dir": directory, "name": name,
                                        "offset": int(offset), "total": int(total)})
        return self._call("PUT", f"/api/fed/files/upload?{query}", data,
                          {"Content-Type": "application/octet-stream"}, timeout=120)

    def rename(self, path: str, new_name: str) -> dict:
        body = json.dumps({"path": path, "new_name": new_name}).encode()
        return self._call("POST", "/api/fed/files/rename", body,
                          {"Content-Type": "application/json"}, timeout=30)

    def delete(self, path: str, recursive: bool = False) -> dict:
        body = json.dumps({"path": path, "recursive": bool(recursive)}).encode()
        return self._call("POST", "/api/fed/files/delete", body,
                          {"Content-Type": "application/json"}, timeout=60)

    def zip(self, paths: list[str], base: str = "") -> bytes:
        body = json.dumps({"paths": list(paths), "base": base}).encode()
        return self._call("POST", "/api/fed/files/zip", body,
                          {"Content-Type": "application/json"}, timeout=180, raw=True)

    def create_session(self, name: str, path: str = "", command: list[str] | None = None,
                       create_dir: bool = True, keep_open: bool = True) -> dict:
        body = json.dumps({"name": name, "path": path, "command": command or [],
                           "create_dir": bool(create_dir), "keep_open": bool(keep_open)}).encode()
        return self._call("POST", "/api/fed/sessions", body,
                          {"Content-Type": "application/json"}, timeout=60)

    def close_pane(self, pane_id: str) -> dict:
        return self._call("POST", f"/api/fed/panes/{urllib.parse.quote(pane_id, safe='')}/close",
                          b"", {"Content-Type": "application/json"}, timeout=30)

    def close_session(self, name: str) -> dict:
        body = json.dumps({"name": name}).encode()
        return self._call("POST", "/api/fed/sessions/close", body,
                          {"Content-Type": "application/json"}, timeout=30)

    def terminal_url(self, pane_id: str, cols: int, rows: int) -> str:
        """给终端用的 WS 地址（令牌放 query：WS 客户端不方便带自定义头）。"""
        query = urllib.parse.urlencode({"token": self.token, "cols": int(cols), "rows": int(rows)})
        base = (self.base_url or self.fallback_url).replace("https://", "wss://").replace(
            "http://", "ws://")
        return f"{base}/api/fed/ws/terminal/{urllib.parse.quote(pane_id, safe='')}?{query}"

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

    read_only = False

    _panes_shared: dict[str, tuple[float, list[dict]]] = {}

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
        # 每个请求都会新建一个 FedTransport，所以缓存要按节点放在类上，
        # 否则每次 validate_pane 都要去问一次对端的窗格列表
        shared = FedTransport._panes_shared.get(self.host_id)
        if shared and now - shared[0] < 5:
            self._panes_cache = shared
            return [dict(pane) for pane in shared[1]]
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
        FedTransport._panes_shared[self.host_id] = self._panes_cache
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

    # -- 写操作：等价于在对端敲键盘（需要 input 作用域，对端自己校验）-------
    def send(self, pane_id: str, text: str, submit: bool = True, pane: dict | None = None) -> bool:
        payload = self.client.input(self.raw_pane(pane_id), text, submit)
        return payload.get("submitted", True)

    def send_key(self, pane_id: str, key: str, pane: dict | None = None) -> dict:
        return self.client.key(self.raw_pane(pane_id), key)

    def send_keys(self, pane_id: str, keys) -> dict:
        for key in keys:
            self.send_key(pane_id, key)
        return {"status": "sent", "keys": list(keys)}

    # 管理会话（admin）：建会话、杀窗格。对端自己还会再查一次政策开关。
    def create_session(self, name: str, path: str = "", command: list[str] | None = None,
                       create_dir: bool = True, keep_open: bool = True) -> dict:
        return self.client.create_session(name, path, command, create_dir, keep_open)

    def kill_pane(self, pane_id: str) -> dict:
        return self.client.close_pane(self.raw_pane(pane_id))

    def kill_session(self, name: str) -> dict:
        return self.client.close_session(name)

    # -- 文件：子节点的文件是主机级的，和 SSH 主机一样 ---------------------
    def list_files(self, directory: str, show_hidden: bool = False, limit: int = 2000) -> dict:
        return self.client.files(directory, show_hidden, limit)

    def file_size(self, path: str) -> int | None:
        try:
            return int(self.client.stat(path).get("size") or 0)
        except FederationError:
            return None

    def download_file(self, path: str, max_bytes: int) -> bytes:
        payload = self.client.download(path)
        if len(payload) > max_bytes:
            raise FederationError(f"文件超过上限（{len(payload)} > {max_bytes} 字节）")
        return payload

    def upload_chunk(self, part: str, data: bytes, offset: int) -> int:
        directory, name = os.path.split(part)
        answer = self.client.upload(directory or "/tmp", name, data, offset=offset)
        return int(answer.get("offset") or answer.get("size") or 0)

    def finish_upload(self, part: str, path: str) -> None:
        self.client.rename(part, os.path.basename(path))

    def upload_file(self, path: str, data: bytes) -> None:
        directory, name = os.path.split(path)
        self.client.upload(directory or "/tmp", name, data)

    def rename_path(self, path: str, new_name: str) -> str:
        return str(self.client.rename(path, new_name).get("path") or "")

    def delete_path(self, path: str, recursive: bool = False) -> str:
        return str(self.client.delete(path, recursive).get("path") or "")

    def zip_files(self, paths, base: str, max_bytes: int, max_files: int) -> bytes:
        if len(paths) > max_files:
            raise FederationError(f"一次最多打包 {max_files} 个文件")
        payload = self.client.zip(list(paths), base)
        if len(payload) > max_bytes:
            raise FederationError(f"打包结果超过上限（{len(payload)} > {max_bytes} 字节）")
        return payload

    def find_suffix(self, base: str, relative: str, max_depth: int = 4, limit: int = 5) -> list[str]:
        return []

    def stream_file(self, path: str, max_bytes: int, chunk: int = 262144,
                    idle_timeout: float = 90.0):
        """按块把对端的文件转发出去：慢链路也不会被"总时长"误判成失败。"""
        query = urllib.parse.urlencode({"path": path})
        errors = []
        client = self.client
        for base in client._urls():
            request = urllib.request.Request(base + f"/api/fed/files/download?{query}")
            request.add_header("Authorization", f"Bearer {client.token}")
            request.add_header("User-Agent", USER_AGENT)
            try:
                response = urllib.request.urlopen(request, timeout=max(idle_timeout, 60))
            except urllib.error.HTTPError as exc:
                errors.append(f"{base}: HTTP {exc.code}")
                if exc.code in (401, 403, 404):
                    break
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{base}: {exc}")
                continue
            sent = 0
            with response:
                while True:
                    piece = response.read(chunk)
                    if not piece:
                        return
                    sent += len(piece)
                    if sent > max_bytes:
                        raise FederationError(f"文件超过上限（> {max_bytes} 字节）")
                    yield piece
            return
        raise FederationError("; ".join(errors) or "node has no address")

    # -- 内部 -------------------------------------------------------------
    def raw_pane(self, pane_id: str) -> str:
        """`<node>~%12` → `%12`：对端只认自己的编号。"""
        return pane_id.split("~", 1)[1] if "~" in pane_id else pane_id


class FedTerminalSession:
    """父节点侧：把子节点的终端（WS）包成和本地 PTY 一样的 read/write/resize/close。

    PTY 走 Cloudflare 时延迟会翻倍，所以终端只在真的需要敲命令时才用；日常聊天仍然走 capture。
    """

    def __init__(self, node: dict, pane_id: str, cols: int = 100, rows: int = 30):
        self.client = client_for(node)
        self.pane_id = pane_id
        self.cols = cols
        self.rows = rows
        self.connection = None
        self.queue: asyncio.Queue | None = None
        self.reader = None

    async def connect(self) -> None:
        import websockets                      # requirements 里已经有（uvicorn 的 WS 实现）
        self.queue = asyncio.Queue()
        self.connection = await websockets.connect(
            self.client.terminal_url(self.pane_id, self.cols, self.rows),
            max_size=None, open_timeout=15, ping_interval=20)
        self.reader = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for message in self.connection:
                payload = message if isinstance(message, (bytes, bytearray)) else str(message).encode()
                await self.queue.put(bytes(payload))
        except Exception:                      # 对端断开就是流结束
            pass
        finally:
            await self.queue.put(b"")

    async def read(self) -> bytes:
        return await self.queue.get()

    async def write(self, data: bytes) -> None:
        if self.connection:
            await self.connection.send(json.dumps(
                {"type": "input", "data": data.decode("utf-8", "replace")}))

    async def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self.connection:
            await self.connection.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass


def client_for(node: dict) -> FedClient:
    """线路顺序就是节点上存的那两条（用户选谁优先就谁优先），文件也一样。"""
    return FedClient(node.get("base_url") or "", node.get("fallback_url") or "",
                     node.get("token") or "")
