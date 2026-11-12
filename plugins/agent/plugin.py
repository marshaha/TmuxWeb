"""The agent transport plugin: a remote host can run commands through a
small agent program instead of over SSH.

The agent (``plugins/agent/agent.py``) lives on the remote host and calls
back to ``/ws/agent`` over the Tailscale network with a per-host token.
The server side:

* ``/ws/agent`` — the callback: peer check, hello + token check, then
  batched exec requests and replies (see ``transport.py`` for the
  protocol notes).
* ``GET /agent/agent.py`` — the agent program itself, public, no secrets.
* ``POST /api/hosts/{id}/agent-token`` — issue/rotate the host's token.
* ``DELETE /api/hosts/{id}/agent-token`` — revoke it, and drop any live
  connection for that host at once.
* ``POST /api/hosts/{id}/agent-install`` — one-click install over the
  existing SSH channel: push the program, write the token, start it.
* ``GET /api/hosts/{id}/agent-status`` — online / last seen / last transport.

Network policy (the whole point of the tailnet rule): the agent link
between this host and a sub server runs over the Tailscale virtual LAN
only. Install commands carry ``ws://<this host's Tailscale IPv4>:8787`` —
never the public domain, never a public IP — and ``/ws/agent`` refuses
any peer outside the tailnet CIDR (default 100.64.0.0/10) plus loopback.
The SSH transport and browser access over the public domain are
untouched by this rule: it constrains the agent link only.

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
import ipaddress
import os
import re
import shlex
import subprocess
import time

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from plugin_api import Plugin
from remote_hosts import SSHRunner
from transport import AgentRegistry, AgentTransport, AgentUnavailable  # noqa: F401  (re-exported for tests)

# The SSH runner used by one-click install. A module-level name (not a
# closure local) so tests can swap in a recording fake without touching SSH.
_ssh_runner_factory = SSHRunner

# Tailscale's CGNAT range: every tailnet IPv4 lives here. The peer check
# and the install addresses both use it; override with
# TMUX_WEB_AGENT_TAILNET_CIDR when the tailnet is configured otherwise.
DEFAULT_TAILNET_CIDR = "100.64.0.0/10"
# The uvicorn port the agent dials on the tailnet (no TLS: WireGuard
# already encrypts the link, and an IP has no TLS certificate). Override
# with TMUX_WEB_AGENT_PORT when the service listens elsewhere.
DEFAULT_AGENT_PORT = 8787


def _tailnet_network(cidr):
    """Parse a CIDR, falling back to the Tailscale default when it is junk."""
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return ipaddress.ip_network(DEFAULT_TAILNET_CIDR)


def _peer_allowed(host, cidr=DEFAULT_TAILNET_CIDR):
    """True when a ``/ws/agent`` peer may connect: tailnet CIDR or loopback.

    The check runs on the direct TCP peer (``websocket.client.host``),
    never on X-Forwarded-For: that header is forgeable and rewritten by
    Cloudflare, so it cannot say where the bytes came from. Loopback is
    allowed because a local agent (and the loopback test rig) needs it;
    loopback is not a public address. Anything unparseable is denied.
    """
    if not isinstance(host, str) or not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
        if ip.is_loopback:
            return True
    network = _tailnet_network(cidr)
    if ip.version != network.version:
        return False
    return ip in network


def _tailscale_ipv4(explicit=None, cidr=DEFAULT_TAILNET_CIDR):
    """This host's Tailscale IPv4, or None when it is not on a tailnet.

    An explicit address (``TMUX_WEB_AGENT_TAILSCALE_IP``) wins, but it
    must be an IPv4 address inside the tailnet CIDR — anything else is a
    config error, raised loudly. Otherwise ask the tailscale CLI, then
    read the address off the tailscale0 interface.
    """
    network = _tailnet_network(cidr)
    if explicit:
        try:
            addr = ipaddress.ip_address(str(explicit).strip())
        except ValueError:
            raise ValueError(
                "TMUX_WEB_AGENT_TAILSCALE_IP is not an IP address: %r" % explicit)
        if not isinstance(addr, ipaddress.IPv4Address) or addr not in network:
            raise ValueError(
                "TMUX_WEB_AGENT_TAILSCALE_IP %s is not inside the tailnet %s"
                % (explicit, network))
        return str(addr)
    candidates = []
    try:
        proc = subprocess.run(["tailscale", "ip", "-4"],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            candidates.extend(proc.stdout.split())
    except (OSError, subprocess.SubprocessError):
        pass
    if not candidates:
        try:
            proc = subprocess.run(["ip", "-4", "-o", "addr", "show", "tailscale0"],
                                  capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                candidates.extend(
                    match.group(1)
                    for match in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)/",
                                             proc.stdout))
        except (OSError, subprocess.SubprocessError):
            pass
    for candidate in candidates:
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv4Address) and addr in network:
            return str(addr)
    return None


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
        # Tailnet policy, all plain attributes so tests can override them:
        # the CIDR /ws/agent peers must come from, an explicit Tailscale
        # IPv4 for this host (None = auto-detect), and the port the agent
        # dials on the tailnet. setup() fills them from the environment.
        self.tailnet_cidr = DEFAULT_TAILNET_CIDR
        self.tailscale_ip = None
        self.agent_port = DEFAULT_AGENT_PORT

    def agent_urls(self):
        """``(ws_url, program_url)`` for the agent, both on the tailnet.

        Raises RuntimeError when this host has no Tailscale IPv4: install
        and token issue fail loudly instead of silently falling back to a
        public address.
        """
        try:
            ip = _tailscale_ipv4(self.tailscale_ip, self.tailnet_cidr)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if ip is None:
            raise RuntimeError(
                "this host is not on the Tailscale network: no Tailscale "
                "IPv4 found (set TMUX_WEB_AGENT_TAILSCALE_IP to override)")
        return ("ws://%s:%d/ws/agent" % (ip, self.agent_port),
                "http://%s:%d/agent/agent.py" % (ip, self.agent_port))

    def setup(self, ctx):
        plugin = self
        store = ctx.services.remote_hosts
        registry = self.registry
        log = ctx.log
        app = ctx.app
        self.agent_file = ctx.directory / "agent.py"
        self.tailnet_cidr = os.environ.get("TMUX_WEB_AGENT_TAILNET_CIDR",
                                           DEFAULT_TAILNET_CIDR)
        try:
            self.tailnet_cidr = str(ipaddress.ip_network(self.tailnet_cidr,
                                                          strict=False))
        except ValueError:
            log.warning("agent: bad TMUX_WEB_AGENT_TAILNET_CIDR %r, using %s",
                        self.tailnet_cidr, DEFAULT_TAILNET_CIDR)
            self.tailnet_cidr = DEFAULT_TAILNET_CIDR
        self.tailscale_ip = os.environ.get("TMUX_WEB_AGENT_TAILSCALE_IP") or None
        try:
            self.agent_port = int(os.environ.get(
                "TMUX_WEB_AGENT_PORT", str(DEFAULT_AGENT_PORT)))
        except ValueError:
            log.warning("agent: bad TMUX_WEB_AGENT_PORT %r, using %d",
                        os.environ.get("TMUX_WEB_AGENT_PORT"),
                        DEFAULT_AGENT_PORT)
            self.agent_port = DEFAULT_AGENT_PORT

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
            # Tailnet only, checked on the direct TCP peer before anything
            # else: a public address never gets to say hello.
            peer = websocket.client.host if websocket.client else None
            if not _peer_allowed(peer, plugin.tailnet_cidr):
                log.warning("agent connection refused from %s: not on the tailnet",
                            peer)
                await websocket.close(code=4403)
                return
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
            # Resolve the tailnet addresses first: without a Tailscale IPv4
            # the token could never be used, so fail loudly instead of
            # issuing (and rotating) a token that points nowhere.
            try:
                ws_url, program_url = plugin.agent_urls()
            except RuntimeError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=503)
            token = await asyncio.to_thread(store.issue_agent_token, host_id)
            # The log names the host only — the token value never appears here.
            log.info("agent token issued for host %s", host_id)
            return {"host_id": host_id, "token": token,
                    "server": ws_url, "program_url": program_url}

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
            # The agent dials back over the tailnet only. Resolve first, so
            # a host with no Tailscale IPv4 fails here — before any token
            # is issued or anything is pushed over SSH.
            try:
                ws_url, _program_url = plugin.agent_urls()
            except RuntimeError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=503)

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
                    % (shlex.quote(agent_path), shlex.quote(ws_url),
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
