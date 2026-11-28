"""Real Codex App Server approvals, with a local model and a fixture observer record."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from websockets import unix_connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_forms
from plugins.agent.hooks.observe import observation, record


PATCH = "*** Begin Patch\n*** Add File: one.txt\n+one\n*** Add File: two.txt\n+two\n*** End Patch"


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        def event(kind, **data):
            self.wfile.write(("data: " + json.dumps(dict(type=kind, **data)) + "\n\n").encode())
        response = {"id":"resp_fixture", "object":"response", "created_at":int(time.time()), "model":"fixture",
                    "output":[], "status":"in_progress"}
        event("response.created", response=response)
        output = []
        if not self.server.called:
            self.server.called = True
            arguments = json.dumps(self.server.arguments)
            item = {"type":"function_call", "name":"exec_command", "arguments":arguments, "call_id":"call_fixture",
                    "id":"fc_fixture", "status":"completed"}
            event("response.output_item.added", output_index=0, item=dict(item, arguments="", status="in_progress"))
            event("response.function_call_arguments.delta", output_index=0, item_id="fc_fixture", delta=arguments)
            event("response.function_call_arguments.done", output_index=0, item_id="fc_fixture", arguments=arguments)
            event("response.output_item.done", output_index=0, item=item)
            output.append(item)
        event("response.completed", response=dict(response, status="completed", output=output,
              usage={"input_tokens":8, "output_tokens":8, "total_tokens":16}))


async def scenario(binary, file_change):
    with tempfile.TemporaryDirectory(dir="/tmp",prefix="cxchat-") as directory, ThreadingHTTPServer(("127.0.0.1",0), Model) as model:
        model.arguments = ({"cmd":"apply_patch <<'PATCH'\n" + PATCH + "\nPATCH"} if file_change else
                           {"cmd":"echo fixture", "sandbox_permissions":"require_escalated",
                            "justification":"Run the isolated fixture?"})
        model.called = False
        threading.Thread(target=model.serve_forever, daemon=True).start()
        home = Path(directory)
        root = home/".codex"
        socket = root/"app-server-control/app-server-control.sock"
        socket.parent.mkdir(parents=True)
        (root/"config.toml").write_text('model="fixture"\nmodel_provider="fixture"\napproval_policy="on-request"\n'
            'sandbox_mode="read-only"\n[model_providers.fixture]\nname="fixture"\nwire_api="responses"\n'
            f'base_url="http://127.0.0.1:{model.server_port}/v1"\n')
        env = {"HOME":directory, "CODEX_HOME":str(root), "PATH":os.defpath, "NO_PROXY":"127.0.0.1,localhost",
               "HTTPS_PROXY":"http://127.0.0.1:1", "HTTP_PROXY":"http://127.0.0.1:1"}
        app = await asyncio.create_subprocess_exec(binary, "app-server", "--listen", "unix://"+str(socket),
                env=env, cwd=directory, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL)
        try:
            while not socket.exists():
                if app.returncode is not None:
                    raise RuntimeError("Fixture app-server failed to start")
                await asyncio.sleep(.05)
            async with unix_connect(str(socket), "ws://localhost", compression=None) as ws:
                cli = codex_forms.RequestChannel(ws)
                await cli.rpc("initialize", {"clientInfo":{"name":"tmuxweb_fixture","version":"1"},
                                             "capabilities":{"experimentalApi":True}})
                await ws.send(json.dumps({"method":"initialized"}))
                start = await cli.rpc("thread/start", {"cwd":directory,"approvalPolicy":"on-request","sandbox":"read-only"})
                thread = start["thread"]["id"]
                turn = await cli.rpc("turn/start", {"threadId":thread,"input":[{"type":"text","text":"fixture","text_elements":[]}]})
                while not cli.pending:
                    await cli.receive()
                request = next(iter(cli.pending.values()))
                method = "item/fileChange/requestApproval" if file_change else "item/commandExecution/requestApproval"
                assert request["method"] == method, request["method"]
                hook = observation("codex", "PermissionRequest", {"tool_name":"exec_command", "turn_id":turn["turn"]["id"],
                                                                 "thread_id":thread, "tool_input":model.arguments})
                record(home/".config/tmuxweb-agent/interactions.sqlite3", "%77", "12345", "codex", hook, time.time())
                def snapshot():
                    return "scrolled", "shared title", directory
                while True:
                    result = await codex_forms.on_host(home, "%77", "12345", snapshot)
                    if result["status"] == "open":
                        break
                    await asyncio.sleep(.15)
                form = result["form"]
                if file_change:
                    detail = form["questions"][0]["description"]
                    assert detail.index("two.txt") < detail.index("+one") and "+two" in detail
                result = await codex_forms.on_host(home, "%77", "12345", snapshot,
                    {"revision":form["revision"], "answers":{"decision":{"indices":[1]}}})
                assert result["status"] == "resolved", result
                assert not (home/"one.txt").exists()
                print("PASS", method, "replayed and declined in private app-server", flush=True)
        finally:
            if app.returncode is None:
                app.terminate()
            try:
                await asyncio.wait_for(app.wait(), 3)
            except TimeoutError:
                app.kill()
                await app.wait()
            model.shutdown()


if __name__ == "__main__":
    for files in (False, True):
        asyncio.run(asyncio.wait_for(scenario(sys.argv[1], files), 45))
