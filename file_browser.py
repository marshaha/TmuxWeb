"""Directory browsing and zipping helpers for local panes and SSH remote hosts.

The same file runs in two places, like ``transcript_reader.py``:

* imported by ``tmux_web.py`` for panes on the local host;
* piped over SSH to a remote ``python3`` (``--list <dir>``) so remote panes get
  the same file tree.

Keep it standard-library only and free of type annotations so an older remote
interpreter can execute the uploaded copy.
"""
import io
import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

MAX_ENTRIES = 5000
DEFAULT_ZIP_FILES = 1000
DEFAULT_ZIP_BYTES = 128 * 1024 * 1024
TYPE_NAMES = {
    "dir": "dir",
    "file": "file",
    "link": "link",
}


class FileBrowserError(RuntimeError):
    pass


def check_path(value):
    """Reject control characters and empty names before touching the filesystem."""
    text = str(value or "")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise FileBrowserError("invalid path")
    return text


def resolve_path(relative, base=None):
    """Resolve a user supplied path against ``base`` (the pane's directory)."""
    text = check_path(relative).strip()
    path = Path(text or ".").expanduser()
    if not path.is_absolute():
        if base is None:
            raise FileBrowserError("relative path without a base directory")
        path = Path(check_path(base)).expanduser() / path
    return path


def _entry(directory, name):
    path = os.path.join(directory, name)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        kind = "dir"
    elif stat.S_ISLNK(mode):
        kind = "link"
    elif stat.S_ISREG(mode):
        kind = "file"
    else:
        kind = "other"
    if kind == "link":
        try:
            kind = "dir" if os.path.isdir(path) else "link"
        except OSError:
            pass
    return {
        "name": name,
        "path": path,
        "type": kind,
        "size": 0 if kind == "dir" else int(info.st_size),
        "mtime": float(info.st_mtime),
        "mode": oct(stat.S_IMODE(mode))[2:].rjust(4, "0"),
    }


def list_directory(directory, show_hidden=False, limit=MAX_ENTRIES):
    """List one directory: folders first, then files, both name-sorted."""
    directory = Path(check_path(str(directory))).expanduser()
    if not directory.exists():
        raise FileBrowserError("directory does not exist: %s" % directory)
    if not directory.is_dir():
        raise FileBrowserError("not a directory: %s" % directory)
    try:
        names = os.listdir(directory)
    except OSError as exc:
        raise FileBrowserError(str(exc))
    entries = []
    truncated = False
    for name in names:
        if not show_hidden and name.startswith("."):
            continue
        entry = _entry(str(directory), name)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    if len(entries) > limit:
        entries = entries[:limit]
        truncated = True
    absolute = os.path.realpath(directory)
    parent = os.path.dirname(absolute)
    return {
        "path": absolute,
        "parent": parent if parent != absolute else None,
        "entries": entries,
        "truncated": truncated,
    }

def _arc_name(path, base):
    """Archive path: relative to ``base`` when possible, else just the name."""
    path = Path(path)
    if base is not None:
        try:
            relative = path.relative_to(base)
        except ValueError:
            relative = None
        if relative is not None:
            text = relative.as_posix()
            if text and text != ".":
                return text
    return path.name


def _drop_nested(paths):
    """Keep only the outermost selections: a chosen directory covers its children."""
    kept = []
    for path in sorted((Path(item) for item in paths), key=lambda item: len(str(item))):
        if any(str(path) == str(parent) or str(path).startswith(str(parent) + os.sep) for parent in kept):
            continue
        kept.append(path)
    return kept


def zip_paths(paths, base=None, max_bytes=DEFAULT_ZIP_BYTES, max_files=DEFAULT_ZIP_FILES):
    """Zip the given files/directories into memory; directories are walked recursively."""
    if not paths:
        raise FileBrowserError("no files selected")
    base_path = Path(check_path(base)).expanduser().resolve() if base else None
    resolved = []
    for raw in paths:
        path = resolve_path(raw, str(base_path) if base_path else None)
        if not path.exists():
            raise FileBrowserError("no such file or directory: %s" % raw)
        resolved.append(path.resolve())
    roots = _drop_nested(resolved)

    buffer = io.BytesIO()
    count = 0
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for root in roots:
            name = _arc_name(root, base_path)
            if root.is_dir():
                archive.writestr(name.rstrip("/") + "/", b"")
                for current, directories, files in os.walk(str(root)):
                    directories.sort()
                    for entry in sorted(files):
                        source = Path(current, entry)
                        if not source.is_file():
                            continue  # sockets, fifos, dangling links
                        count += 1
                        if count > max_files:
                            raise FileBrowserError("selection has more than %d files" % max_files)
                        total += source.stat().st_size
                        if total > max_bytes:
                            raise FileBrowserError("selection is larger than the %d byte zip limit" % max_bytes)
                        inner = _arc_name(source, base_path)
                        archive.write(str(source), inner)
            else:
                count += 1
                if count > max_files:
                    raise FileBrowserError("selection has more than %d files" % max_files)
                total += root.stat().st_size
                if total > max_bytes:
                    raise FileBrowserError("selection is larger than the %d byte zip limit" % max_bytes)
                archive.write(str(root), name)
    return buffer.getvalue()


def tar_to_zip(payload, max_bytes=DEFAULT_ZIP_BYTES, max_files=DEFAULT_ZIP_FILES):
    """Re-container a ``tar`` stream pulled from a remote host into a zip archive.

    Remote hosts only need ``tar`` (universal) instead of ``zip``; the bytes come
    back in one SSH call and are converted here, where the parser lives.
    """
    buffer = io.BytesIO()
    count = 0
    total = 0
    try:
        source = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as exc:
        raise FileBrowserError("remote returned an unreadable archive: %s" % exc)
    with source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in source:
            if member.isdir():
                archive.writestr(member.name.rstrip("/") + "/", b"")
                continue
            if not member.isfile():
                continue
            handle = source.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            count += 1
            if count > max_files:
                raise FileBrowserError("selection has more than %d files" % max_files)
            total += len(data)
            if total > max_bytes:
                raise FileBrowserError("selection is larger than the %d byte zip limit" % max_bytes)
            archive.writestr(member.name, data)
    return buffer.getvalue()


def main(argv):
    import argparse

    parser = argparse.ArgumentParser(description="List a directory as JSON")
    parser.add_argument("--list", dest="directory", required=True)
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--limit", type=int, default=MAX_ENTRIES)
    args = parser.parse_args(argv)
    try:
        result = list_directory(args.directory, show_hidden=args.hidden, limit=args.limit)
    except (FileBrowserError, OSError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
    else:
        payload = {"ok": True, **result}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
