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
        from tmux_web import RelayAskRequest, RelayMessageRequest, RelayTimeoutError, retry_or_fail

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
            # The manager keys by the resolved pane ref, so a later chat send
            # to the same pane supersedes this retry (and vice versa).
            try:
                resend_key = services.remote_hosts.pane_ref(pane.get("host_id") or "localhost", pane_id)
            except (AttributeError, ValueError):
                resend_key = payload.target
            resend = getattr(services, "resend", None)
            if resend is not None:
                resend.cancel(resend_key, "superseded")
            try:
                submitted = await asyncio.to_thread(transport.send, pane_id, payload.text, payload.submit)
            except (services.tmux_error, RuntimeError, OSError, ValueError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if submitted is None:
                submitted = True
            # The paste landed but Enter never moved it: hand it to the
            # background retry instead of failing it, and keep the 200 -
            # the text did arrive, so a 503 would be a lie.
            retrying = False
            status = "sent"
            if payload.submit and not submitted:
                status, retrying = retry_or_fail(resend, resend_key, payload.text)
            services.relay_log.appendleft({
                "at": time.time(), "target": payload.target, "source": "message",
                "status": status, "question": payload.text[:400], "elapsed": 0,
            })
            return {"status": status, "target": payload.target, "host": host_name,
                    "submitted": submitted, "retrying": retrying}

        @app.get("/api/relay/log")
        async def relay_history(request: Request):
            auth(request)
            return {"events": list(services.relay_log)}
