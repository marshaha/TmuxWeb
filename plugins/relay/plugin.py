"""The cross-server relay HTTP surface, as a plugin.

The plumbing lives in the core (it needs the tmux backends, the SSH runners and the hosts snapshot); what
moved here is the *feature*: its four endpoints. Disable the plugin and they are simply not there, which is
the point of the plugin system — a feature you can leave out.

The MCP side of the relay (``mcp_relay.py``) still calls ``services.relay_ask`` directly, so the two share
one implementation.
"""

import asyncio
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from plugin_api import Plugin


class RelayPlugin(Plugin):
    def setup(self, ctx):
        # Imported inside setup (tmux_web is what loads us) — that works only because this module does NOT
        # enable `from __future__ import annotations`: FastAPI resolves handler annotations against the
        # module globals, and a local name would not be found there (the body would be read as a query arg).
        from tmux_web import RelayAskRequest, RelayMessageRequest, RelayTimeoutError

        services = ctx.services
        app = ctx.app
        auth = services.require_auth

        @app.get("/api/relay/peers")
        async def relay_peers(request: Request):
            auth(request)
            return {"peers": await asyncio.to_thread(services.collect_pane_states)}

        @app.post("/api/relay/ask")
        async def relay_ask(request: Request, payload: RelayAskRequest):
            auth(request)
            try:
                return await services.relay_ask(payload)
            except RelayTimeoutError as exc:
                return JSONResponse({"detail": str(exc), "tail": exc.tail, "target": payload.target},
                                    status_code=504)

        @app.post("/api/relay/message")
        async def relay_message(request: Request, payload: RelayMessageRequest):
            auth(request)
            transport, pane_id, pane, host_name = await asyncio.to_thread(services.relay_target, payload.target)
            try:
                await asyncio.to_thread(transport.send, pane_id, payload.text, True)
            except (services.tmux_error, RuntimeError, OSError, ValueError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            services.relay_log.appendleft({
                "at": time.time(), "target": payload.target, "source": "message",
                "status": "sent", "question": payload.text[:400], "elapsed": 0,
            })
            return {"status": "sent", "target": payload.target, "host": host_name}

        @app.get("/api/relay/log")
        async def relay_history(request: Request):
            auth(request)
            return {"events": list(services.relay_log)}
