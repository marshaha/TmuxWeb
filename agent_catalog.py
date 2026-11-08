"""Which coding agents a host can actually run.

The console lets you start a tmux session *with* an agent, so it has to know what
is installed where.  Probing is deliberately dumb: a single POSIX ``sh`` snippet
that asks the shell for each binary on PATH plus the usual per-user install
directories.  No ``python3`` on the far side (a remote host may only have sshd +
tmux), no version calls (``claude --version`` alone costs a few hundred ms and
macOS has no ``timeout`` to bound it).
"""

import os
import shlex
import shutil
from pathlib import Path

# name -> what to look for.  Order is the order the UI shows.
AGENTS = (
    {"name": "hermes", "label": "Hermes", "bins": ("hermes",)},
    {"name": "claude", "label": "Claude Code", "bins": ("claude",)},
    {"name": "codex", "label": "Codex", "bins": ("codex",)},
    {"name": "kimi", "label": "Kimi Code", "bins": ("kimi",)},
    {"name": "opencode", "label": "OpenCode", "bins": ("opencode",)},
    {"name": "gemini", "label": "Gemini CLI", "bins": ("gemini",)},
)

BY_NAME = {spec["name"]: spec for spec in AGENTS}

# Install locations that a non-interactive shell often misses: tmux starts a
# login-ish shell, but the PATH it inherits may predate a pip/npm install.
# ``$HOME`` (not ``~``) because these are quoted inside the probe script.
EXTRA_DIRS = ("$HOME/.local/bin", "$HOME/.kimi-code/bin", "$HOME/.opencode/bin",
              "/opt/homebrew/bin", "/usr/local/bin")

# Prints "<name>\t<path>" per hit; safe on macOS's sh (no bashisms, no timeout).
PROBE_SCRIPT = "".join([
    'for spec in %s; do\n' % " ".join("%s:%s" % (spec["name"], spec["bins"][0]) for spec in AGENTS),
    '  name=${spec%%:*}; bin=${spec##*:}; path=$(command -v "$bin" 2>/dev/null)\n',
    '  if [ -z "$path" ]; then\n',
    '    for dir in %s; do\n' % " ".join('"%s"' % item for item in EXTRA_DIRS),
    '      if [ -x "$dir/$bin" ]; then path="$dir/$bin"; break; fi\n',
    '    done\n',
    '  fi\n',
    '  [ -n "$path" ] && printf "%s\\t%s\\n" "$name" "$path"\n',
    'done\n',
    'exit 0\n',
])


def spec_for(agent: str) -> dict | None:
    """Case-insensitive lookup; ``None`` means "no such agent"."""
    return BY_NAME.get((agent or "").strip().lower())


def parse_probe(text: str) -> list[dict]:
    """Turn the probe output into ``[{name, label, path}]``, in catalog order."""
    found = {}
    for line in (text or "").splitlines():
        name, _, path = line.partition("\t")
        name = name.strip().lower()
        path = path.strip()
        if name in BY_NAME and path:
            found[name] = path
    return [{"name": spec["name"], "label": spec["label"], "path": found[spec["name"]]}
            for spec in AGENTS if spec["name"] in found]


def probe_local() -> list[dict]:
    """Same answer as PROBE_SCRIPT, resolved in-process."""
    found = {}
    for spec in AGENTS:
        for binary in spec["bins"]:
            hit = shutil.which(binary)
            if not hit:
                for directory in EXTRA_DIRS:
                    candidate = Path(os.path.expandvars(directory)).expanduser() / binary
                    if candidate.is_file() and candidate.stat().st_mode & 0o111:
                        hit = str(candidate)
                        break
            if hit:
                found[spec["name"]] = hit
                break
    return [{"name": spec["name"], "label": spec["label"], "path": found[spec["name"]]}
            for spec in AGENTS if spec["name"] in found]


def command_for(agent: str, args_text: str = "", binary: str = "") -> list[str]:
    """``agent`` + its free-form arguments, as an argv list.

    ``binary`` is the absolute path the probe found on *that* host.  Using it
    matters: a tmux pane does not necessarily inherit the PATH that ``command -v``
    saw over ssh, so a bare ``claude`` can fail with 127 and take the whole
    session down with it.

    Raises ``ValueError`` for an unknown agent or unbalanced quoting, both of
    which the caller turns into a 422 instead of starting something surprising.
    """
    spec = spec_for(agent)
    if spec is None:
        raise ValueError("unknown agent: %s" % (agent or "(empty)"))
    try:
        extra = shlex.split(args_text or "")
    except ValueError as exc:
        raise ValueError("agent arguments are not valid shell words: %s" % exc) from exc
    head = (binary or "").strip() or spec["name"]
    return [head, *extra]


def render(command: list[str]) -> str:
    """One shell-safe string, which is what ``tmux new-session`` wants."""
    return shlex.join(command)


def with_keep_open(command: list[str]) -> str:
    """Agent command + a shell that survives it.

    An agent started in a tmux session *is* the session: if it exits (no PATH,
    not logged in, a typo in the arguments) tmux closes the session and the user
    is left staring at a console that "created something" and then lost it.  Keep
    the pane, print why it ended, and drop into a shell.
    """
    name = shlex.quote(command[0] if command else "agent")
    return (f"{render(command)}; __tmuxweb_rc=$?; "
            "printf '\n[%s 已退出，code %s] 会话保留，可直接继续输入\n' "
            f"{name} \"$__tmuxweb_rc\"; exec \"${{SHELL:-bash}}\"")
