"""Private local socket for Chat sessions that own a harness stdio connection."""
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess


MAX_FRAME = 4 * 1024 * 1024
MAX_FORM = 48 * 1024


def endpoint(pid, provider="kimi"):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", str(pid)):
        raise ValueError("invalid pane PID")
    folder = {"kimi": "kimi-acp", "claude": "claude-chat"}[provider]
    return Path.home() / ".local/state/tmuxweb" / folder / (str(pid) + ".sock")


def available(pid, provider="kimi"):
    try:
        target = os.readlink(endpoint(pid, provider))
        owner = re.fullmatch(r"owner-([1-9][0-9]*)[.]sock", target)
        return bool(owner and is_owner_process(pid, int(owner[1]), provider))
    except (ValueError,OSError,IndexError):
        return False


def peer(sock):
    pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise ValueError("different user")
    return pid


def foreground_owner(pane_pid, owner):
    def process(pid):
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    pane, process_info = process(pane_pid), process(owner)
    # session, controlling tty and foreground process group of this exact pane.
    return (pane[4] != "0" and pane[3:5] == process_info[3:5]
            and pane[5] == process_info[2] and int(pane[5]) > 0)


def is_owner_process(pane_pid, owner, provider="kimi"):
    argv = Path(f"/proc/{owner}/cmdline").read_bytes().split(b"\0")
    name = {"kimi": "kimi_acp.py", "claude": "claude_chat.py"}[provider]
    script = Path(__file__).with_name(name).resolve()
    return os.fsencode(script) in argv[:2] and foreground_owner(pane_pid,owner)


def query(pid, pane, action=None, provider="kimi"):
    sending = isinstance(action,dict) and "prompt" in action
    absent = {"status":"absent" if sending else "closed"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            try:
                sock.connect(str(endpoint(pid, provider)))
            except (ConnectionRefusedError, FileNotFoundError):
                return absent
            owner_pid = peer(sock)
            if not is_owner_process(pid, owner_pid, provider):
                return absent
            sock.sendall(encode({"pane": pane, "action": action}))
            with sock.makefile("rb") as stream:
                reply = json.loads(stream.readline(MAX_FRAME))
            return reply if isinstance(reply, dict) else {"status":"unavailable"}
    except (OSError, ValueError, IndexError):
        return {"status":"unavailable" if sending else "closed"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def string(value, limit=8192):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("missing or oversized text")
    return value


def listener_paths(provider):
    pane = os.environ.get("TMUX_PANE","")
    if not re.fullmatch(r"%[0-9]+", pane):
        raise ValueError("需要在 tmux pane 中启动结构化 Chat")
    pid = subprocess.check_output(["tmux","display-message","-p","-t",pane,"#{pane_pid}"],text=True).strip()
    path = endpoint(pid, provider)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = path.parent.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
        raise ValueError("Chat socket 目录需要归当前用户所有，权限为 0700")
    if not foreground_owner(pid, os.getpid()):
        raise ValueError("启动进程与 pane 前台不匹配")
    if available(pid, provider):
        raise ValueError("此 pane 已有 结构化 Chat 会话")
    path.unlink(missing_ok=True)
    listener = path.with_name(f"owner-{os.getpid()}.sock")
    listener.unlink(missing_ok=True)
    return pane, pid, path, listener
