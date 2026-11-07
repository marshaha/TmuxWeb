"""Which model an agent pane is running, and how to switch it.

Every agent CLI we support takes the same slash command inside its TUI:

    /model <id>

so "pick a model in the chat" is a text send, not a new protocol.  What differs
is the *list*: models come and go per vendor, so the list here is a short set of
sensible presets plus whatever the session itself reports as current; the UI also
lets the user type any id by hand.
"""

# Presets are deliberately short: they are the ids that were current when this
# was written.  "自定义" in the UI covers everything else.
# 值就是各 CLI 自己的 `/model` 参数：claude 认可 opus/sonnet/haiku 这类别名，
# kimi 认的是 /model 菜单里显示的名字（K3、K3-256k、K3.1 Pro …），codex 认模型 id。
# 清单只是几个常用项，"自定义" 覆盖其余；真正权威的是 agent 自己的 /model 菜单。
PRESETS = {
    "claude": ("opus", "sonnet", "haiku"),
    "codex": ("gpt-5.6-sol", "gpt-5-codex", "o3"),
    "kimi": ("K3.1 Pro", "K3", "K3-256k", "K2.7 Code Highspeed"),
    "hermes": (),
}

# How to ask that CLI to switch model.  Kimi Code only understands the
# argument-less form: `/model <anything>` answers "Unknown model alias", so we
# open its own picker and let the user choose there.
SWITCH_TEMPLATES = {
    "claude": "/model {model}",
    "codex": "/model {model}",
    "hermes": "/model {model}",
    "kimi": "/model",
}
PICKER_ONLY = {"kimi"}


# 下拉里按 provider 分组显示：模型属于哪家，是切模型时最先要看清的东西。
PROVIDER_LABELS = {
    "claude": "Anthropic",
    "codex": "OpenAI",
    "kimi": "Kimi Code",
    "hermes": "Hermes",
}


def provider_label(provider: str, configured: str = "") -> str:
    """Group heading for the picker: the vendor, or the route Hermes is using."""
    configured = (configured or "").strip()
    if configured:
        return configured
    return PROVIDER_LABELS.get((provider or "").lower(), (provider or "").capitalize())


def providers() -> tuple[str, ...]:
    return tuple(PRESETS)


def preset_for(provider: str) -> list[str]:
    return list(PRESETS.get((provider or "").lower(), ()))


def picker_only(provider: str) -> bool:
    return (provider or "").lower() in PICKER_ONLY


def switch_command(provider: str, model: str = "") -> str:
    """The exact line to type into the pane."""
    provider = (provider or "").lower()
    template = SWITCH_TEMPLATES.get(provider)
    if template is None:
        raise ValueError("this pane is not an agent: cannot switch its model")
    if "{model}" not in template:
        return template            # picker-only providers ignore the argument
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    if len(model) > 80 or any(ch in model for ch in "\r\n\t"):
        raise ValueError("that model id is not usable")
    return template.format(model=model)


def hermes_route(home=None) -> str:
    """`model.provider` from ~/.hermes/config.yaml (the route Hermes is on).

    Read with a regex instead of a YAML parser: the console's venv does not ship
    PyYAML, and we only need the one scalar.
    """
    import os
    import re
    from pathlib import Path

    home = home or os.path.expanduser("~")
    path = Path(home) / ".hermes" / "config.yaml"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    inside = False
    for line in text.splitlines():
        if not inside:
            if re.match(r"^model:\s*$", line):
                inside = True
            continue
        if line and not line[0].isspace():
            break                      # 下一个顶层键，model 块结束
        found = re.match(r'^\s+provider:\s*"?([\w./-]+)"?', line)
        if found:
            return found.group(1)
    return ""


def supported(provider: str) -> bool:
    return (provider or "").lower() in SWITCH_TEMPLATES
