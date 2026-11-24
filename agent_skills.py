"""Read installed skill manifest metadata on the pane's host (stdlib only)."""
import itertools
import json
import os
from pathlib import Path
import re
import stat
import sys


PROVIDERS = ("codex", "claude", "hermes", "kimi")
NAME = re.compile(r"[\w][\w.-]{0,95}")


def roots(provider, cwd):
    home = Path.home()

    def profile(variable, default):
        return Path(os.environ.get(variable) or home / default) / "skills"

    project = Path(cwd) if cwd and Path(cwd).is_absolute() else None
    parents = []
    if project and project.is_dir():
        for directory in (project, *project.parents):
            if directory == home or directory == directory.parent:
                break
            parents.append(directory)
            if (directory / ".git").exists():
                break
        if parents and not (parents[-1] / ".git").exists():
            parents = [project]

    if provider == "codex":
        installed = profile("CODEX_HOME", ".codex")
        return [p / ".agents/skills" for p in parents] + [home / ".agents/skills", installed, installed / ".system"]
    if provider == "claude":
        return [p / ".claude/skills" for p in parents] + [profile("CLAUDE_CONFIG_DIR", ".claude")]
    if provider == "kimi":
        return ([parents[-1] / sub for sub in (".kimi-code/skills", ".agents/skills")] if parents else []) + [
            profile("KIMI_CODE_HOME", ".kimi-code"), home / ".agents/skills"]
    if provider == "hermes":
        # Hermes controls project-skill trust separately from filesystem visibility.
        return [profile("HERMES_HOME", ".hermes")]
    return []


def manifest_fields(path):
    # A manifest can be provided by an untrusted checkout. Never block on a FIFO
    # or follow a manifest symlink to a different file.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        lines = stream.read(8192).decode("utf-8-sig", "replace").splitlines()
    fields = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                return fields
            key, sep, value = line.partition(":")
            if sep and key in ("name", "description", "user-invocable", "type"):
                fields[key] = value.partition(" #")[0].strip().strip("\"'")
        return None
    return fields


def catalog(provider, cwd):
    found = {}
    visited = set()
    budget = 4096

    def walk(folder, depth):
        nonlocal budget
        if budget <= 0 or len(found) >= 512:
            return
        try:
            identity = folder.stat()
            inode = (identity.st_dev, identity.st_ino)
            if inode in visited:
                return
            visited.add(inode)
            with os.scandir(folder) as entries:
                children = sorted(itertools.islice(entries, budget), key=lambda e: e.name)
            budget -= len(children)
        except OSError:
            return
        for entry in children:
            if entry.name.startswith(".") or len(found) >= 512:
                continue
            path = Path(entry.path)
            try:
                if entry.is_dir():
                    manifest = path / "SKILL.md"
                    if not manifest.is_file():
                        if depth and not entry.is_symlink():
                            walk(path, depth - 1)
                        continue
                    fallback = path.name
                elif provider == "kimi" and path.suffix == ".md":
                    manifest, fallback = path, path.stem
                else:
                    continue
                fields = manifest_fields(manifest)
            except OSError:
                continue
            if fields is None or fields.get("user-invocable", "").lower() == "false":
                continue
            if provider == "kimi" and fields.get("type", "prompt") not in ("prompt", "inline", "flow"):
                continue
            if provider in ("codex", "kimi") and manifest.name == "SKILL.md" and not (
                fields.get("name") and fields.get("description")
            ):
                continue
            name = fields.get("name", fallback)
            if NAME.fullmatch(name):
                found.setdefault(name, None)

    for folder in roots(provider, cwd):
        walk(folder, 4 if provider == "hermes" else 0)
    return sorted(found, key=str.casefold)


if __name__ == "__main__":
    print(json.dumps(catalog(*sys.argv[1:]), ensure_ascii=False))
