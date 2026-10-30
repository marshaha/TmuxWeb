"""A copy-me plugin: one authenticated endpoint, one config knob, one shutdown hook.

Copy this folder, rename it, edit plugin.toml, and add ``TMUX_WEB_PLUGINS=your-name`` (or ``all``).

Things worth knowing:

* ``ctx.app`` is the FastAPI app — register routes on it like anywhere else.
* ``ctx.require_auth(request)`` is the same session check the console uses. **Call it**: plugins run with
  full access, and an unauthenticated endpoint here would expose the machine.
* ``ctx.config`` is ``config.json`` in this folder merged with ``TMUX_WEB_PLUGIN_EXAMPLE_CONFIG`` (JSON).
* ``ctx.data_dir`` is a writable folder for this plugin (created on demand).
* ``ctx.services`` is the documented core: ``tmux``, ``remote_hosts``, ``home_store``, ``relay_log``,
  ``relay_ask``, ``relay_target``, ``collect_pane_states``, ``tmux_error``. Anything else in the codebase is
  an implementation detail and may move.
"""

from __future__ import annotations

import time

from fastapi import Request

from plugin_api import Plugin


class ExamplePlugin(Plugin):
    def setup(self, ctx) -> None:
        started = time.time()
        ctx.log.info("example plugin ready (data dir: %s)", ctx.data_dir)

        @ctx.app.get("/api/example/ping")
        async def ping(request: Request):
            ctx.require_auth(request)
            return {
                "plugin": ctx.name,
                "version": ctx.version,
                "greeting": ctx.config.get("greeting", "hello"),
                "uptime_seconds": round(time.time() - started, 1),
                "panes": len(ctx.services.tmux.list_panes()) if ctx.services.tmux else None,
            }

        ctx.on_shutdown(lambda: ctx.log.info("example plugin stopping"))
