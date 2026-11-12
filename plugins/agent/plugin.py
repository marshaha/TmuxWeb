"""The agent transport plugin: a remote host can run commands through a
small agent program instead of over SSH.

The agent (``plugins/agent/agent.py``) lives on the remote host and calls
back to ``/ws/agent`` over WSS with a per-host token. The server side:

* ``/ws/agent`` — the callback: hello + token check, then batched exec
  requests and replies (see ``transport.py`` for the protocol notes).
* ``GET /agent/agent.py`` — the agent program itself, public, no secrets.
* ``POST /api/hosts/{id}/agent-token`` — issue/rotate the host's token.
* ``DELETE /api/hosts/{id}/agent-token`` — revoke it, and drop any live
  connection for that host at once.
* ``POST /api/hosts/{id}/agent-install`` — one-click install over the
  existing SSH channel: push the program, write the token, start it.
* ``GET /api/hosts/{id}/agent-status`` — online / last seen / last transport.

Transport selection is a plugin hook (see ``plugin_api``): hosts with
``transport == "agent"`` get an :class:`AgentTransport`, everything else
keeps the plain :class:`SSHRunner`. An agent transport falls back to SSH
whenever the agent is not connected, and records which transport actually
carried each operation.

The interactive terminal (``/ws/terminal``) is deliberately NOT moved:
v1 only swaps the command transport, and a terminal session keeps its SSH.

Tokens are never logged: the issue/rotate/revoke logs name the host only.
The token itself travels to the remote host only as ``input_bytes`` of one
``cat`` — never inside a command string, and never in a log line.
"""

import asyncio
import shlex
import time

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from plugin_api import Plugin
from remote_hosts import SSHRunner
from transport import AgentRegistry, AgentTransport, AgentUnavailable  # noqa: F401  (re-exported for tests)

# The SSH runner used by one-click install. A module-level name (not a
# closure local) so tests can swap in a recording fake without touching SSH.
_ssh_runner_factory = SSHRunner


def _wss_url(request: Request) -> str:
    """``wss://<this request's host>/ws/agent`` — the agent calls back here."""
    host = (request.headers.get("host") or "").strip() or request.url.netloc
    return "wss://%s/ws/agent" % host


class AgentPlugin(Plugin):
    """Holds the registry; ``setup`` wires the routes and the core hooks."""

    def __init__(self) -> None:
        self.registry = AgentRegistry()
        # How long a fresh /ws/agent connection may take to say hello.
        # A plain attribute (not a constant) so tests can shrink it.
        self.hello_timeout = 10.0
        self.agent_file = None            # set in setup(); tests may point it elsewhere
        self.runner_factory = None        # host -> runner, also registered with the core
        self.host_status = None           # host_id -> dict, also registered with the core

    def setup(self, ctx):
        plugin = self
        store = ctx.services.remote_hosts
        registry = self.registry
        log = ctx.log
        app = ctx.app
        self.agent_file = ctx.directory / "agent.py"

        def agent_runner_factory(host):
            """Hosts that opted into the agent get AgentTransport, the rest SSH."""
            if (host or {}).get("transport") == "agent":
                return AgentTransport(host, registry)
            return SSHRunner(host)

        def agent_host_status(host_id):
            return registry.status(host_id)

        self.runner_factory = agent_runner_factory
        self.host_status = agent_host_status
        if ctx.manager is not None:
            ctx.manager.register_runner_factory(agent_runner_factory)
            ctx.manager.register_host_status(agent_host_status)
        ctx.on_shutdown(registry.shutdown)

        # -- the callback --------------------------------------------------
        @app.websocket("/ws/agent")
        async def agent_socket(websocket: WebSocket):
            await websocket.accept()
            try:
                hello = await asyncio.wait_for(
                    websocket.receive_json(), timeout=plugin.hello_timeout)
            except asyncio.TimeoutError:
                await websocket.close(code=4408)
                return
            except Exception:
                await websocket.close(code=4400)
                return
            if (not isinstance(hello, dict) or hello.get("type") != "hello"
                    or not isinstance(hello.get("token"), str)):
                await websocket.close(code=4403)
                return
            host_id = store.verify_agent_token(hello["token"])
            if host_id is None:
                await websocket.close(code=4403)
                return
            try:
                host = store.get_host(host_id)
            except KeyError:
                await websocket.close(code=4403)
                return
            connection = registry.register(host_id, websocket)
            now = time.time()
            registry.touch(host_id, now)
            try:
                store.touch_agent_last_seen(host_id, now)
            except (KeyError, AttributeError, OSError, ValueError):
                pass
            await websocket.send_json({
                "type": "welcome",
                "host_id": host_id,
                "host_name": host.get("name") or host_id,
            })
            log.info("agent connected for host %s", host_id)
            try:
                while True:
                    message = await websocket.receive_json()
                    registry.touch(host_id)
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") == "heartbeat":
                        try:
                            store.touch_agent_last_seen(host_id, time.time())
                        except (KeyError, AttributeError, OSError, ValueError):
                            pass
                        continue
                    request_id = message.get("id")
                    if request_id is not None:
                        registry.deliver(host_id, request_id, message)
            except WebSocketDisconnect:
                pass
            except Exception:
                log.warning("agent connection for host %s ended with error",
                            host_id, exc_info=True)
            finally:
                registry.unregister(host_id, connection)
                log.info("agent disconnected for host %s", host_id)

        # -- the agent program itself (public: code, no secrets) -----------
        @app.get("/agent/agent.py")
        async def agent_program(request: Request):
            try:
                data = plugin.agent_file.read_bytes()
            except OSError:
                return JSONResponse({"detail": "agent program is not available"},
                                    status_code=404)
            return PlainTextResponse(data.decode("utf-8"),
                                     headers={"Cache-Control": "no-store"})

        # -- tokens ---------------------------------------------------------
        @app.post("/api/hosts/{host_id}/agent-token")
        async def issue_agent_token(request: Request, host_id: str):
            ctx.require_auth(request)
            try:
                store.get_host(host_id)
            except KeyError:
                return JSONResponse({"detail": "remote host not found"}, status_code=404)
            token = await asyncio.to_thread(store.issue_agent_token, host_id)
            # The log names the host only — the token value never appears here.
            log.info("agent token issued for host %s", host_id)
            return {"host_id": host_id, "token": token}

        @app.delete("/api/hosts/{host_id}/agent-token")
        async def revoke_agent_token(request: Request, host_id: str):
            ctx.require_auth(request)
            try:
                store.get_host(host_id)
            except KeyError:
                return JSONResponse({"detail": "remote host not found"}, status_code=404)
            had_token = await asyncio.to_thread(store.revoke_agent_token, host_id)
            registry.disconnect(host_id)     # a live agent loses its connection now
            log.info("agent token revoked for host %s", host_id)
            return {"host_id": host_id, "status": "revoked", "had_token": bool(had_token)}

        # -- one-click install over SSH --------------------------------------
        @app.post("/api/hosts/{host_id}/agent-install")
        async def install_agent(request: Request, host_id: str):
            ctx.require_auth(request)
            try:
                host = store.get_host(host_id, include_credential=True)
            except KeyError:
                return JSONResponse({"detail": "remote host not found"}, status_code=404)
            wss_url = _wss_url(request)

            def _install():
                try:
                    agent_bytes = plugin.agent_file.read_bytes()
                except OSError as exc:
                    raise RuntimeError("agent program is not available: %s" % exc) from exc
                # Issuing rotates: a failed install still retires the old
                # token, which is the safe side — a half-installed agent
                # must not keep a token the server no longer recognises.
                token = store.issue_agent_token(host_id)
                ssh = _ssh_runner_factory(host)
                home = ssh.run('printf %s "$HOME"', timeout=15)
                if home.returncode != 0:
                    raise RuntimeError(
                        home.stderr.decode(errors="replace").strip()
                        or "could not resolve the remote home directory")
                base = home.stdout.decode(errors="replace").strip()
                if not base:
                    raise RuntimeError("could not resolve the remote home directory")
                agent_path = "%s/.tmuxweb-agent/agent.py" % base
                token_path = "%s/.config/tmuxweb-agent/token" % base
                ssh.upload_file(agent_path, agent_bytes)
                # The token travels as stdin bytes only: it never appears in
                # any command string, and therefore never in a log line.
                written = ssh.run(
                    "mkdir -p -- %s && cat > %s"
                    % (shlex.quote("%s/.config/tmuxweb-agent" % base),
                       shlex.quote(token_path)),
                    input_bytes=token.encode(), timeout=30)
                if written.returncode != 0:
                    raise RuntimeError(
                        written.stderr.decode(errors="replace").strip()
                        or "could not write the agent token")
                done = ssh.run(
                    "python3 %s install --server %s --token-file %s"
                    % (shlex.quote(agent_path), shlex.quote(wss_url),
                       shlex.quote(token_path)),
                    timeout=120)
                if done.returncode != 0:
                    raise RuntimeError(
                        done.stderr.decode(errors="replace").strip()
                        or done.stdout.decode(errors="replace").strip()
                        or "agent install failed")
                return {"status": "installed", "transport": "agent"}

            try:
                result = await asyncio.to_thread(_install)
            except (OSError, RuntimeError, ValueError) as exc:
                log.warning("agent install failed for host %s: %s", host_id, exc)
                return JSONResponse({"detail": str(exc)}, status_code=503)
            try:
                store.update_host(host_id, transport="agent")
            except (KeyError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=503)
            log.info("agent installed for host %s", host_id)
            return result

        # -- status -----------------------------------------------------------
        @app.get("/api/hosts/{host_id}/agent-status")
        async def agent_status(request: Request, host_id: str):
            ctx.require_auth(request)
            try:
                host = store.get_host(host_id)
            except KeyError:
                return JSONResponse({"detail": "remote host not found"}, status_code=404)
            payload = {"transport": host.get("transport", "ssh")}
            payload.update(registry.status(host_id))
            return payload
