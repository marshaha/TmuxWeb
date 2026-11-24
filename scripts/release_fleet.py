"""Prepare a tested release and roll it out to configured TmuxWeb supervisors."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def checks(directory, ui):
    py = (directory / "python.log").read_text()
    js = (directory / "node.log").read_text()
    if not (directory / "PASS").is_file() or not re.search(r"^OK(?: \(skipped=\d+\))?\s*$", py, re.M):
        raise ValueError("missing successful Python test result")
    if not re.search(r"^[ℹ#] fail 0$", js, re.M):
        raise ValueError("missing successful Node test result")
    result = {"python": re.search(r"^Ran (\d+) tests?", py, re.M)[1],
              "node": re.search(r"^[ℹ#] pass (\d+)$", js, re.M)[1]}
    for name in ui:
        if not re.fullmatch(r"[a-z_]+", name):
            raise ValueError("invalid browser check name")
        widths = re.findall(r"^(\d+)\s*px pass$", (directory / (name + ".log")).read_text(), re.M)
        if sorted(map(int, widths)) != [390, 768, 834, 900, 1024, 1180, 1440]:
            raise ValueError(f"incomplete browser coverage: {name}")
        result[name] = widths
    return result


def prepare(args):
    tested = args.tested.resolve()
    base = args.baseline.resolve()
    if tested in (base, ROOT) or not tested.is_relative_to(Path("/tmp")):
        raise ValueError("use an isolated /tmp test worktree")
    names = set(args.files) | {"VERSION", "CHANGELOG.md"}
    if any(n.startswith("web/") for n in names) and not args.browser:
        raise ValueError("specify the seven-width browser check for this UI release")
    release = (ROOT / "VERSION").read_text().strip()
    previous = (base / "VERSION").read_text().strip()
    for version in (previous, release):
        if not re.fullmatch(SEMVER, version):
            raise ValueError("expected MAJOR.MINOR.PATCH")
    if tuple(map(int, release.split("."))) <= tuple(map(int, previous.split("."))):
        raise ValueError(f"release must advance beyond {previous}")
    if f"## {release}\n" not in (ROOT / "CHANGELOG.md").read_text():
        raise ValueError("missing release notes")
    entries = {}
    for name in sorted(names):
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_./-]*", name) or ".." in Path(name).parts or str(Path(name)) != name:
            raise ValueError(f"invalid package path: {name}")
        paths = [folder / name for folder in (ROOT, tested, base)]
        if any(p.resolve() != p or (p.exists() and not p.is_file()) for p in paths):
            raise ValueError(f"expected regular file: {name}")
        after, verified, before = map(digest, paths)
        if after != verified or (after is None and before is None):
            raise ValueError(f"test copy differs or path is unknown: {name}")
        entries[name] = {"before": before, "after": after}
    if args.prior_changelog:
        notes = (ROOT / "CHANGELOG.md").read_text().splitlines()
        prior = set()
        for changelog in args.prior_changelog:
            if any(line not in notes for line in changelog.read_text().splitlines() if line.startswith("- ")):
                raise ValueError("merge the other node's release notes before packaging")
            prior.add(digest(changelog))
        entries["CHANGELOG.md"]["prior"] = sorted(prior)
    head = git(base, "rev-parse", "HEAD")
    manifest = {"installer_sha": digest(ROOT / "scripts/release_node.py"), "release": release, "previous": previous, "head": head, "files": entries,
                "version": release + "+" + git(base, "rev-parse", "--short", "HEAD"),
                "console_sha": digest(ROOT / "web/assets/console-app.js")}
    build = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    manifest.update(build=build, checks=checks(args.checks, args.browser))
    stage = Path(tempfile.mkdtemp(prefix=f"tmuxweb-{release}-{build}-"))
    with tarfile.open(stage / "release.tar.gz", "w:gz") as tar:
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        tar.add(stage / "manifest.json", "manifest.json")
        tar.add(ROOT / "scripts/release_node.py", "release_node.py")
        for name, entry in entries.items():
            if entry["after"]:
                tar.add(ROOT / name, "payload/" + name)
    print(stage / "release.tar.gz")


# Each node keeps the immutable package and its pre-release files outside the checkout.
UPLOAD = """set -eu
umask 077
store="$HOME/.local/state/tmuxweb/releases"
mkdir -p "$store"
tmp=$(mktemp -d "$store/.incoming.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
cat > "$tmp/release.tar.gz"
echo "$3  $tmp/release.tar.gz" | sha256sum -c - >/dev/null
tar -xzf "$tmp/release.tar.gz" -C "$tmp"
echo "$2" > "$tmp/build"
exec 9>"$store/.upload.lock"
flock 9
if test -d "$store/$1" && test "$(cat "$store/$1/build")" != "$2"; then
    if test -e "$store/$1/previous.json" || test -e "$store/$1/deployment.json" || test "$(cat "$4/VERSION")" = "$1"; then
        echo "release number already used" >&2; exit 1
    fi
    hold=$(mktemp -d "$store/.preflight-$1.XXXXXXXX")
    mv "$store/$1" "$hold/package"
fi
if test -e "$store/$1"; then
    echo "$3  $store/$1/release.tar.gz" | sha256sum -c - >/dev/null || { echo "retry with the archived package" >&2; exit 1; }
else
    mv "$tmp" "$store/$1"
fi
"""
RUN = """set -eu
stage="$HOME/.local/state/tmuxweb/releases/$1"
cd "$2"
set -a
. ./.env
set +a
exec ./.venv/bin/python "$stage/release_node.py" "$3"
"""


def on(node, script, args, stream=None):
    cmd = ["bash", "-c", script, "release", *args]
    if node.get("ssh"):
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "--", node["ssh"], shlex.join(cmd)]
    return subprocess.check_output(cmd, stdin=stream if stream else subprocess.DEVNULL).decode().strip()


def deploy(args):
    nodes = json.loads(args.config.read_text())["nodes"]
    if not nodes or len({(n.get("ssh"), n["root"]) for n in nodes}) != len(nodes) or len({n["name"] for n in nodes}) != len(nodes):
        raise ValueError("configure distinct fleet nodes")
    with tarfile.open(args.archive) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
        expected = {"release_node.py": digest(ROOT / "scripts/release_node.py"), "manifest.json": None}
        for n, entry in manifest["files"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_./-]*", n) or ".." in Path(n).parts or str(Path(n)) != n:
                raise ValueError("invalid path in manifest")
            if entry["after"] is not None:
                expected["payload/" + n] = entry["after"]
        for item in tar:
            if item.name not in expected or not item.isfile():
                raise ValueError("unexpected archive member: " + item.name)
            want = expected.pop(item.name)
            if want and hashlib.sha256(tar.extractfile(item).read()).hexdigest() != want:
                raise ValueError("archive content mismatch: " + item.name)
        if expected:
            raise ValueError("missing archive members")
    release, build = manifest["release"], manifest["build"]
    if not re.fullmatch(SEMVER, release) or not re.fullmatch(r"[0-9a-f]{16}", build):
        raise ValueError("invalid release identifier")
    core = {key: value for key, value in manifest.items() if key not in ("build", "checks")}
    if (hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16] != build
            or manifest["installer_sha"] != digest(ROOT / "scripts/release_node.py")):
        raise ValueError("release manifest does not match build identifier or installer")
    sha = digest(args.archive)
    store = Path.home() / ".local/state/tmuxweb/releases"
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (store / ".fleet.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        def phase(node, action):
            raw = on(node, RUN, [release, node["root"], action])
            result = json.loads(raw)
            print(f"{node['name']} {action}: {result}", flush=True)
            return result
        status = {}
        for node in nodes:
            with args.archive.open("rb") as stream:
                on(node, UPLOAD, [release, build, sha, node["root"]], stream)
            status[node["name"]] = phase(node, "preflight")
        activated = []
        try:
            for node in nodes:
                if status[node["name"]] == "ready":
                    activated.append(node)
                phase(node, "activate")
            for node in nodes:
                phase(node, "verify")
        except BaseException:
            for node in reversed(activated):
                try:
                    phase(node, "rollback")
                except Exception as exc:
                    print(f"Rollback requires attention on {node['name']}: {exc}", file=sys.stderr)
            raise
        receipt = {"release": release, "build": build, "archive_sha256": sha, "nodes": [n["name"] for n in nodes],
                   "checks": manifest["checks"], "verified_at": datetime.now(timezone.utc).isoformat()}
        for dest in (store / release / "deployment.json", store / "last-deployment.json"):
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temp = dest.with_suffix(".tmp")
            temp.write_text(json.dumps(receipt, indent=2) + "\n")
            os.replace(temp, dest)
        print(f"Released {release} ({build})")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--tested", required=True, type=Path)
    p.add_argument("--checks", required=True, type=Path)
    p.add_argument("--prior-changelog", action="append", type=Path, default=[])
    p.add_argument("--browser", action="append", default=[])
    p.add_argument("files", nargs="+")
    d = sub.add_parser("deploy")
    d.add_argument("--config", type=Path, default=Path.home()/".config/tmuxweb-release/fleet.json")
    d.add_argument("archive", type=Path)
    args = cli.parse_args()
    (prepare if args.command == "prepare" else deploy)(args)
