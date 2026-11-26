"""File-only credentials for native MCP. Issued and revoked by the server owner."""
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import shlex


PREFIX = "twfile_"


def directory():
    return Path.home() / ".config/tmuxweb/file-grants"


def issue(target, handle, pid, cwd):
    if not (re.fullmatch(r"(?:[0-9a-f]+~)?%[0-9]+", target) and re.fullmatch(r"#[0-9]+", handle)
            and str(pid).isdigit() and os.path.isabs(cwd)):
        raise ValueError("expected an exact pane, handle, PID and absolute working directory")
    token = PREFIX + secrets.token_hex(32)
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    directory().mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory() / fingerprint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict(target=target, handle=handle, pid=str(pid), cwd=cwd), f)
    return token


def lookup(token):
    if not re.fullmatch(PREFIX + r"[0-9a-f]{64}", token):
        raise ValueError("invalid file credential")
    try:
        record = json.loads((directory() / hashlib.sha256(token.encode()).hexdigest()).read_text())
        if not isinstance(record, dict) or any(
                not isinstance(record.get(key), str) or not record[key] for key in ("target", "handle", "pid", "cwd")):
            raise ValueError("invalid file grant record")
        return record
    except (OSError, ValueError) as exc:
        raise ValueError("invalid or revoked file credential") from exc


def schema(grant):
    return {"name": "send_file", "description": f"Deliver a file to {grant['handle']} via TmuxWeb. No pane input.",
            "inputSchema": {"type": "object", "properties": {
                "target": {"type": "string", "enum": [grant["handle"], grant["target"]]},
                "name": {"type": "string", "description": "File basename at the destination"},
                "content": {"type": "string", "description": "Standard base64 file bytes"},
                "notify": {"type": "boolean", "enum": [False]}},
                "required": ["target", "name", "content"], "additionalProperties": False}}


def arguments(grant, args, limit):
    if not isinstance(args, dict) or set(args) - {"target", "content", "name", "notify"}:
        raise ValueError("unsupported file arguments")
    if args.get("target") not in (grant["target"], grant["handle"]) or args.get("notify", False) is not False:
        raise ValueError("credential only allows silent delivery to the assigned pane")
    name, content = args.get("name"), args.get("content")
    if (not isinstance(name, str) or not name or name.startswith(".") or "/" in name or "\\" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name.encode()) > 255):
        raise ValueError("invalid destination filename")
    if not isinstance(content, str) or len(content) > 4 * ((limit + 2) // 3):
        raise ValueError("missing content or file exceeds size limit")
    data = base64.b64decode(content, validate=True)
    if len(data) > limit:
        raise ValueError("file exceeds size limit")
    return name, data


def deposit(cwd, name, data, digest):
    """Also executed on the receiving host. Anchor writes to opened directory FDs."""
    import hashlib
    import os
    import secrets
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("incomplete file")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open("/", flags)
    try:
        for part in cwd.split("/"):
            if not part:
                continue
            if part in (".", ".."):
                raise ValueError("invalid destination directory")
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        try:
            os.mkdir("upload_files", 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        dest = os.open("upload_files", flags, dir_fd=parent)
        try:
            temp = ".incoming-" + secrets.token_hex(16)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dest)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.link(temp, name, src_dir_fd=dest, dst_dir_fd=dest, follow_symlinks=False)
            finally:
                os.unlink(temp, dir_fd=dest)
        finally:
            os.close(dest)
    finally:
        os.close(parent)


def deliver(grant, pane, runner, name, data):
    if str(pane.get("pane_pid")) != grant["pid"] or pane.get("path") != grant["cwd"]:
        raise ValueError("target pane or working directory changed; renew the grant")
    cwd = grant["cwd"]
    digest = hashlib.sha256(data).hexdigest()
    if runner is None:
        deposit(cwd, name, data, digest)
    else:
        script = inspect.getsource(deposit) + "\nimport sys\ndeposit(sys.argv[1],sys.argv[2],sys.stdin.buffer.read(),sys.argv[3])"
        reply = runner.run(shlex.join(["python3", "-c", script, cwd, name, digest]), input_bytes=data, timeout=180)
        if reply.returncode:
            if b"FileExistsError" in reply.stderr:
                raise ValueError("destination file already exists")
            raise ValueError("receiver could not save file")
    return {"target": grant["target"], "path": os.path.join(cwd, "upload_files", name), "size": len(data),
            "sha256": digest, "notified": False}
