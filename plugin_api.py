"""Plugin support for TmuxWeb.

A plugin is a directory under ``plugins/`` with a manifest and a Python module:

    plugins/my-thing/plugin.toml
    plugins/my-thing/plugin.py

The manifest:

    name = "my-thing"          # must match the directory name
    version = "1.0.0"
    api = 1                     # the plugin API this was written against
    entry = "plugin.py"
    description = "one line for /api/plugins"
    default_enabled = false     # true only for first-party plugins

and the module exposes a class (``PLUGIN`` or ``Plugin``) derived from :class:`Plugin`:

    from plugin_api import Plugin

    class MyThing(Plugin):
        def setup(self, ctx):
            ctx.log.info("hello")
            ctx.on_shutdown(lambda: ctx.log.info("bye"))

            @ctx.app.get("/api/my-thing/ping")
            async def ping(request):
                ctx.require_auth(request)
                return {"pong": ctx.config.get("greeting", "hi")}

Why so narrow: plugins run **in-process** with full access to the app, so they are only loaded when they
are explicitly enabled, a failing plugin is reported (never fatal), and the documented surface is the
:class:`PluginContext` — everything else is an implementation detail that may change.

Enable them with ``TMUX_WEB_PLUGINS``:

* unset/empty  → only plugins that declare ``default_enabled = true`` (first-party)
* ``all``      → every plugin found on disk
* ``a,b``      → exactly those (``-a`` to turn one off, e.g. ``all,-my-thing``)
* ``none``     → nothing
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import Request      # module level: ``from __future__ import annotations`` makes handlers
                                 # resolve their annotations against this module's globals

API_VERSION = 1


class PluginError(RuntimeError):
    """A plugin could not be loaded; recorded and reported, never fatal."""


# --------------------------------------------------------------------------- context
@dataclass
class PluginServices:
    """The core capabilities a plugin may use. Grows carefully — treat every field as a stable promise."""

    require_auth: Callable[[Any], None]
    plugins_root: str
    data_root: str
    tmux: Any = None
    tmux_error: type[BaseException] = Exception
    remote_hosts: Any = None
    home_store: Any = None
    share_store: Any = None
    relay_log: Any = None
    relay_ask: Callable[..., Any] | None = None
    relay_target: Callable[..., Any] | None = None
    collect_pane_states: Callable[..., Any] | None = None
    version: str = ""


@dataclass
class PluginContext:
    """Everything a plugin is allowed to touch."""

    app: Any                      # the FastAPI application: add routes to it
    name: str
    version: str
    directory: Path
    config: dict[str, Any]
    services: Any                 # PluginServices: the documented core capabilities
    log: logging.Logger
    _shutdown: list[Callable[[], None]] = field(default_factory=list)

    # -- convenience ------------------------------------------------------
    def require_auth(self, request) -> None:
        """Raise HTTP 401 unless the request carries a valid session (same check the console uses)."""
        self.services.require_auth(request)

    @property
    def data_dir(self) -> Path:
        """A per-plugin writable directory (created on demand)."""
        target = Path(self.services.data_root) / "plugins" / self.name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        self._shutdown.append(callback)

    def run_shutdown(self) -> None:
        for callback in reversed(self._shutdown):
            try:
                callback()
            except Exception:                      # a broken hook must not stop the others
                self.log.exception("plugin %s: shutdown hook failed", self.name)


class Plugin:
    """Subclass this. Only ``setup`` is required."""

    def setup(self, ctx: PluginContext) -> None:      # pragma: no cover - interface
        raise NotImplementedError

    def shutdown(self) -> None:
        pass


# --------------------------------------------------------------------------- manifest
@dataclass
class PluginSpec:
    name: str
    directory: Path
    version: str = "0.0.0"
    api: int = API_VERSION
    entry: str = "plugin.py"
    description: str = ""
    default_enabled: bool = False
    error: str = ""

    @property
    def path(self) -> Path:
        return self.directory / self.entry


def read_manifest(directory: Path) -> PluginSpec:
    manifest = directory / "plugin.toml"
    spec = PluginSpec(name=directory.name, directory=directory)
    if not manifest.exists():
        spec.error = "plugin.toml is missing"
        return spec
    try:
        with manifest.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        spec.error = f"plugin.toml is unreadable: {exc}"
        return spec
    spec.name = str(data.get("name") or directory.name)
    spec.version = str(data.get("version") or "0.0.0")
    spec.api = int(data.get("api") or API_VERSION)
    spec.entry = str(data.get("entry") or "plugin.py")
    spec.description = str(data.get("description") or "")
    spec.default_enabled = bool(data.get("default_enabled", False))
    if spec.name != directory.name:
        spec.error = f"manifest name {spec.name!r} does not match directory {directory.name!r}"
    elif spec.api > API_VERSION:
        spec.error = f"needs plugin API {spec.api}, this build speaks {API_VERSION}"
    elif not spec.path.exists():
        spec.error = f"entry {spec.entry} is missing"
    return spec


def parse_enabled(raw: str | None) -> tuple[set[str], set[str], bool]:
    """Turn ``TMUX_WEB_PLUGINS`` into (explicit on, explicit off, load-all)."""
    text = (raw or "").strip()
    if not text:
        return set(), set(), False
    if text.lower() == "none":
        return set(), {"*"}, False
    on: set[str] = set()
    off: set[str] = set()
    load_all = False
    for token in text.replace(";", ",").split(","):
        item = token.strip()
        if not item:
            continue
        if item.startswith("-"):
            off.add(item[1:].strip())
        elif item.lower() == "all":
            load_all = True
        else:
            on.add(item)
    return on, off, load_all


def _plugin_config(directory: Path, name: str) -> dict[str, Any]:
    """``config.json`` in the plugin folder, with ``TMUX_WEB_PLUGIN_<NAME>_CONFIG`` (JSON) merged on top."""
    config: dict[str, Any] = {}
    path = directory / "config.json"
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, ValueError):
            pass
    override = os.environ.get(f"TMUX_WEB_PLUGIN_{name.upper().replace('-', '_')}_CONFIG")
    if override:
        try:
            extra = json.loads(override)
            if isinstance(extra, dict):
                config.update(extra)
        except ValueError:
            pass
    return config


# --------------------------------------------------------------------------- manager
class PluginManager:
    """Finds, loads and reports plugins. One per application."""

    def __init__(self, app, services, *, directory: str | Path | None = None,
                 enabled: str | None = None, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.services = services
        self.directory = Path(directory) if directory else Path(services.plugins_root)
        self.log = logger or logging.getLogger("tmuxweb.plugins")
        self.enabled_raw = enabled if enabled is not None else os.environ.get("TMUX_WEB_PLUGINS", "")
        self.specs: list[PluginSpec] = []
        self.loaded: dict[str, Plugin] = {}
        self.contexts: dict[str, PluginContext] = {}
        self.errors: dict[str, str] = {}

    # -- discovery --------------------------------------------------------
    def discover(self) -> list[PluginSpec]:
        found: list[PluginSpec] = []
        if not self.directory.is_dir():
            self.specs = found
            return found
        for child in sorted(self.directory.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            found.append(read_manifest(child))
        self.specs = found
        return found

    def _wanted(self, spec: PluginSpec) -> bool:
        on, off, load_all = parse_enabled(self.enabled_raw)
        if "*" in off or spec.name in off:
            return False
        if spec.name in on:
            return True
        if on:                      # an explicit list means exactly those, plus nothing by default
            return False
        return load_all or spec.default_enabled

    # -- loading ----------------------------------------------------------
    def load_all(self) -> None:
        for spec in self.discover():
            if spec.error:
                self.errors[spec.name] = spec.error
                self.log.warning("plugin %s skipped: %s", spec.name, spec.error)
                continue
            if not self._wanted(spec):
                continue
            try:
                self._load(spec)
            except Exception as exc:              # one broken plugin must never take the app down
                self.errors[spec.name] = str(exc)
                self.log.exception("plugin %s failed to load", spec.name)

    def _load(self, spec: PluginSpec) -> None:
        module_name = f"tmuxweb_plugin_{spec.name.replace('-', '_')}"
        module_spec = importlib.util.spec_from_file_location(module_name, spec.path)
        if module_spec is None or module_spec.loader is None:
            raise PluginError(f"cannot import {spec.path}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        # let the plugin import its own siblings: plugins/<name>/helper.py
        sys.path.insert(0, str(spec.directory))
        try:
            module_spec.loader.exec_module(module)
        finally:
            try:
                sys.path.remove(str(spec.directory))
            except ValueError:
                pass

        factory = getattr(module, "PLUGIN", None)
        if factory is None:
            # a module that does `from plugin_api import Plugin` has the base class in its namespace, so
            # look for a *subclass* rather than picking Plugin itself (which would raise NotImplementedError)
            factory = next((value for value in vars(module).values()
                            if isinstance(value, type) and issubclass(value, Plugin) and value is not Plugin),
                           None)
        if factory is None:
            raise PluginError("no plugin class found (expected PLUGIN or a Plugin subclass)")
        instance = factory() if isinstance(factory, type) else factory
        if not isinstance(instance, Plugin):
            raise PluginError("the plugin class must derive from plugin_api.Plugin")

        context = PluginContext(app=self.app, name=spec.name, version=spec.version,
                                directory=spec.directory, config=_plugin_config(spec.directory, spec.name),
                                services=self.services, log=self.log.getChild(spec.name))
        instance.setup(context)
        self.loaded[spec.name] = instance
        self.contexts[spec.name] = context
        self.log.info("plugin %s %s loaded", spec.name, spec.version)

    # -- reporting --------------------------------------------------------
    def status(self) -> list[dict]:
        rows = []
        for spec in self.specs:
            error = self.errors.get(spec.name, "")
            rows.append({
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "api": spec.api,
                "enabled": spec.name in self.loaded,
                "first_party": spec.default_enabled,
                "error": error,
            })
        return rows

    def shutdown(self) -> None:
        for name, context in list(self.contexts.items()):
            context.run_shutdown()
            plugin = self.loaded.get(name)
            try:
                plugin.shutdown() if plugin else None
            except Exception:
                self.log.exception("plugin %s: shutdown failed", name)


def install_status_route(app, manager: PluginManager, require_auth: Callable[[Any], None]) -> None:
    """``GET /api/plugins`` — what is installed, what is loaded, what broke."""

    @app.get("/api/plugins")
    async def list_plugins(request: Request):
        require_auth(request)
        return {"api": API_VERSION, "plugins": manager.status()}
