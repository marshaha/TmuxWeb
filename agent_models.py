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


def switch_command(provider: str, model: str = "", route: str = "") -> str:
    """The exact line to type into the pane.

    ``route`` is the provider the model belongs to: Hermes keeps several of them
    in one config and needs ``/model <id> --provider <name>`` to cross over.
    """
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
    route = (route or "").strip()
    if route:
        if len(route) > 60 or any(ch in route for ch in "\r\n\t ") or not all(
                ch.isalnum() or ch in "._-/" for ch in route):
            raise ValueError("that provider name is not usable")
        return template.format(model=model) + " --provider " + route
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


# ---------------------------------------------------------------- Hermes 的模型清单
#
# Hermes 没有"可选模型"的静态列表：每个 provider 都是一条 OpenAI 兼容的
# /v1/models。下拉要列全，就得去问 provider 本人（`hermes model` 也是这么干的）。

MODELS_CACHE_TTL = 900
_MODELS_CACHE = {}


def provider_map(text: str) -> dict:
    """{provider: {base_url, key_env, ...}} from config.yaml's ``providers:`` block."""
    providers = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].startswith("providers:"):
            index += 1
            current = None
            while index < len(lines):
                line = lines[index]
                if line and not line[0].isspace():
                    break                      # 下一个顶层键
                stripped = line.strip()
                index += 1
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                key, _, value = stripped.partition(":")
                value = value.strip().strip('"').strip("'")
                if indent <= 2:
                    current = key
                    providers.setdefault(current, {})
                elif current:
                    providers[current][key] = value
            break
        index += 1
    return providers


def _key_for(entry: dict, environ=None) -> str:
    import os
    environ = os.environ if environ is None else environ
    name = entry.get("key_env") or ""
    if name and environ.get(name):
        return environ[name]
    if name:
        from pathlib import Path
        home = Path(environ.get("HOME") or "~").expanduser()
        try:
            for line in (home / ".hermes" / ".env").read_text(errors="replace").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return entry.get("api_key", "")


def fetch_models(base_url: str, key: str, timeout: float = 8.0) -> list:
    """Ask an OpenAI-compatible endpoint for its model ids."""
    import json
    import urllib.request

    if not base_url:
        return []
    headers = {"User-Agent": "TmuxWeb/1.0"}   # 有的网关直接 403 掉 python-urllib 的默认 UA
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(base_url.rstrip("/") + "/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception:  # noqa: BLE001 - a dead provider must not break the picker
        return []
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    names = []
    for item in items:
        name = (item or {}).get("id") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def hermes_models(home=None, runner=None, route: str = "", ttl: int = MODELS_CACHE_TTL) -> list:
    """Models behind the provider Hermes is currently pointed at (cached)."""
    import time

    home = home or ""
    if runner is None:
        cache_key = ("local", route)
    else:
        host = getattr(runner, "host_id", None) or getattr(runner, "host", {}).get("id") \
            or getattr(runner, "address", "") or str(id(runner))
        cache_key = (host, route)
    cached = _MODELS_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < ttl:
        return cached[1]
    if runner is not None:
        names = _remote_models(runner, route)
    else:
        from pathlib import Path
        base = Path(home or "~").expanduser()
        try:
            text = (base / ".hermes" / "config.yaml").read_text(errors="replace")
        except OSError:
            text = ""
        providers = provider_map(text)
        route = route or hermes_route(str(base))
        entry = providers.get(route, {})
        try:
            names = fetch_models(entry.get("base_url", ""), _key_for(entry))
        except Exception:  # noqa: BLE001 - a broken provider must not break the picker
            names = []
    if names:
        _MODELS_CACHE[cache_key] = (now, names)
    return names


REMOTE_MODELS_SCRIPT = r"""
python3 - <<'PYX'
import json, os, re, sys, urllib.request

def block(text, key):
    out, inside = [], False
    for line in text.splitlines():
        if not inside:
            if re.match(r"^%s:\s*$" % key, line):
                inside = True
            continue
        if line and not line[0].isspace():
            break
        out.append(line)
    return out

home = os.path.expanduser("~")
try:
    cfg = open(os.path.join(home, ".hermes", "config.yaml"), errors="replace").read()
except OSError:
    sys.exit(0)

route = os.environ.get("TMUXWEB_ROUTE", "").strip()
providers, current = {}, ""

# model.provider（命令行 --provider 已经在调用前给了就优先，config 只是默认值）
if not route:
    for line in block(cfg, "model"):
        m = re.match(r'\s+provider:\s*"?([\w./-]+)"?', line)
        if m:
            route = m.group(1)
            break

# providers.<route>.{base_url,key_env}
lines = cfg.splitlines()
inside, current = False, ""
for line in lines:
    if not inside:
        if line.startswith("providers:"):
            inside = True
        continue
    if line and not line[0].isspace():
        break
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    indent = len(line) - len(line.lstrip())
    key, _, value = stripped.partition(":")
    value = value.strip().strip('"').strip("'")
    if indent <= 2:
        current = key
        providers.setdefault(current, {})
    elif current:
        providers[current][key] = value

def key_for(entry):
    name = entry.get("key_env", "")
    key = os.environ.get(name, "") if name else ""
    if not key and name:
        try:
            for line in open(os.path.join(home, ".hermes", ".env"), errors="replace"):
                if line.startswith(name + "="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    return key


order = []
for name in ([route] if route else []) + list(providers):
    if name and name in providers and name not in order:
        order.append(name)

for name in order:
    entry = providers.get(name) or {}
    base_url = entry.get("base_url", "")
    if not base_url:
        continue
    headers = {"User-Agent": "TmuxWeb/1.0"}   # 默认 urllib UA 会被部分网关 403
    key = key_for(entry)
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with urllib.request.urlopen(urllib.request.Request(base_url.rstrip("/") + "/models",
                                                           headers=headers), timeout=8) as response:
            payload = json.load(response)
    except Exception:
        continue
    names = [(item or {}).get("id") if isinstance(item, dict) else None
             for item in (payload.get("data") or [])]
    for model in names:
        if model:
            print("%s<::>%s" % (name, model))
PYX
"""


def hermes_groups(home=None, runner=None, route: str = "", ttl: int = MODELS_CACHE_TTL) -> list:
    """[{provider, models}] for every provider in config.yaml (current route first)."""
    import time

    cache_key = (getattr(runner, "host_id", None) or (getattr(runner, "address", "") if runner else "local"), "__all__", route)
    cached = _MODELS_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < ttl:
        return cached[1]

    groups = []
    if runner is not None:
        for line in _remote_group_lines(runner, route):
            name, _, model = line.partition("<::>")
            if not model:
                continue
            if not groups or groups[-1]["provider"] != name:
                groups.append({"provider": name, "models": []})
            groups[-1]["models"].append(model)
    else:
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path

        base = Path(home or "~").expanduser()
        try:
            text = (base / ".hermes" / "config.yaml").read_text(errors="replace")
        except OSError:
            text = ""
        providers = provider_map(text)
        order = ([route] if route else []) + [name for name in providers if name != route]
        order = [name for name in order if name in providers]

        def load(name):
            entry = providers.get(name) or {}
            try:
                return name, fetch_models(entry.get("base_url", ""), _key_for(entry))
            except Exception:  # noqa: BLE001 - one dead provider must not hide the rest
                return name, []

        if order:
            with ThreadPoolExecutor(max_workers=min(6, len(order))) as pool:
                for name, models in pool.map(load, order):
                    if models:
                        groups.append({"provider": name, "models": models})
    if groups:
        _MODELS_CACHE[cache_key] = (now, groups)
    return groups


def _remote_group_lines(runner, route: str = "") -> list:
    script = REMOTE_MODELS_SCRIPT.lstrip("\n")
    command = script
    if route:
        command = "TMUXWEB_ROUTE='%s' " % route.replace("'", "") + script
    try:
        result = runner.run(command, timeout=45)
    except Exception:  # noqa: BLE001 - remote listing is a nicety, never a blocker
        return []
    output = getattr(result, "stdout", b"") or b""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return [line.strip() for line in output.splitlines() if "<::>" in line]


def _remote_models(runner, route: str = "") -> list:
    # 注意 lstrip：脚本常量以换行开头，直接拼前缀会让 `VAR=x` 单独成行（不生效）
    script = REMOTE_MODELS_SCRIPT.lstrip("\n")
    command = script
    if route:
        command = "TMUXWEB_ROUTE='%s' " % route.replace("'", "") + script
    try:
        result = runner.run(command, timeout=25)
    except Exception:  # noqa: BLE001 - remote listing is a nicety, never a blocker
        return []
    output = getattr(result, "stdout", b"") or b""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    models = []
    for line in output.splitlines():
        name, _, model = line.partition("<::>")
        if model.strip():
            models.append(model.strip())
    return models
