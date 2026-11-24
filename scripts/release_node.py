"""Apply one tested TmuxWeb package to an existing run_service.sh checkout."""
import fcntl
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib import parse, request

ROOT = Path.cwd()
STAGE = Path(__file__).resolve().parent
MANIFEST = json.loads((STAGE / "manifest.json").read_text())
FILES = MANIFEST["files"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def install(source, target):
    if source is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmuxweb-release-")
    os.close(fd)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def check_tree():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != MANIFEST["head"]:
        raise RuntimeError("checkout revision differs from package")
    for name, entry in FILES.items():
        target = ROOT / name
        if target.resolve() != target or not target.is_relative_to(ROOT):
            raise RuntimeError(f"unexpected deployment path {name}")
        if digest(target) not in (entry["before"], entry["after"], *entry.get("prior", [])):
            raise RuntimeError(f"independent change in {name}")
        if digest(STAGE / "payload" / name) != entry["after"]:
            raise RuntimeError(f"payload mismatch for {name}")
    supervisor = subprocess.check_output(["tmux", "display-message", "-p", "-t", "=tmuxweb:", "#{pane_start_command}"], text=True)
    if "scripts/run_service.sh" not in supervisor:
        raise RuntimeError("TmuxWeb supervisor missing")


def client():
    base = "http://127.0.0.1:" + str(int(os.environ.get("TMUX_WEB_PORT", "8787")))
    op = request.build_opener(request.ProxyHandler({}), request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with op.open(base + "/login", data=parse.urlencode({"password": os.environ["TMUX_WEB_PASSWORD"]}).encode(), timeout=8):
        pass

    def get(path):
        with op.open(base + path, timeout=15) as reply:
            return reply.read()

    return get


def health(expected):
    get = client()
    info = json.loads(get("/api/version"))
    if (info["release"], info["version"]) != (expected["release"], expected["version"]):
        raise RuntimeError(f"booted version mismatch: {info}")
    if hashlib.sha256(get("/assets/console-app.js")).hexdigest() != expected["console_sha"]:
        raise RuntimeError("served console differs from package")
    home = get("/")
    if b'id="terminal-keys"' not in home or info["version"].encode() not in home:
        raise RuntimeError("console page did not load the expected build")
    return info


def reload_and_check(expected):
    envfile = ROOT / ".env"
    stat = envfile.stat()
    os.utime(envfile, ns=(stat.st_atime_ns, max(time.time_ns(), (int(stat.st_mtime) + 1) * 10**9)))
    deadline = time.monotonic() + 100
    while True:
        try:
            return health(expected)
        except (OSError, ValueError, RuntimeError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("service did not become healthy") from exc
            time.sleep(1)


def restore(saved):
    for name, original in saved["files"].items():
        path = ROOT / name
        current = digest(path)
        if current == original:
            continue
        if current != FILES[name]["after"]:
            raise RuntimeError(f"cannot roll back an independent edit in {name}")
        src = STAGE / "previous" / name
        if digest(src) != original:
            raise RuntimeError(f"rollback copy mismatch for {name}")
        install(src if original else None, path)
    reload_and_check(saved["health"])


def run(phase):
    old = STAGE / "previous.json"
    check_tree()
    if phase == "preflight":
        info = json.loads(client()("/api/version"))
        if info["release"] not in (MANIFEST["previous"], MANIFEST["release"]):
            raise RuntimeError(f"unexpected installed release {info}")
        if info["release"] == MANIFEST["release"]:
            health(MANIFEST)
            if not all(digest(ROOT / n) == e["after"] for n, e in FILES.items()):
                raise RuntimeError("active release has inconsistent files")
            return "already-active"
        if not old.exists() and any(digest(ROOT / n) not in (e["before"], *e.get("prior", [])) for n, e in FILES.items()):
            raise RuntimeError("node does not match the expected release baseline")
        return "ready"
    if phase == "verify":
        return health(MANIFEST)
    if phase == "rollback":
        if not old.exists() and all(digest(ROOT / n) in (e["before"], *e.get("prior", [])) for n, e in FILES.items()):
            return "unchanged"
        restore(json.loads(old.read_text()))
        return "restored"
    if phase != "activate":
        raise ValueError(phase)
    if all(digest(ROOT / n) == e["after"] for n, e in FILES.items()):
        try:
            health(MANIFEST)
        except (OSError, ValueError, RuntimeError):
            if not old.exists():
                raise
            reload_and_check(MANIFEST)
        return "already-active"
    if old.exists():
        saved = json.loads(old.read_text())
    else:
        get = client()
        previous = json.loads(get("/api/version"))
        if previous["release"] != MANIFEST["previous"]:
            raise RuntimeError("another release was installed after preflight")
        previous["console_sha"] = hashlib.sha256(get("/assets/console-app.js")).hexdigest()
        saved = {"health": previous, "files": {}}
        for name in FILES:
            src = ROOT / name
            saved["files"][name] = digest(src)
            if src.exists():
                install(src, STAGE / "previous" / name)
        tmp = old.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved))
        os.replace(tmp, old)
    try:
        for name, entry in FILES.items():
            target = ROOT / name
            current = digest(target)
            if current == entry["after"]:
                continue
            if current != saved["files"][name]:
                raise RuntimeError(f"concurrent change in {name}")
            install(STAGE / "payload" / name if entry["after"] else None, target)
        reload_and_check(MANIFEST)
    except BaseException:
        restore(saved)
        raise
    return "activated"


if __name__ == "__main__":
    with (STAGE.parent / ".node.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run(sys.argv[1])
        print(json.dumps(result))
