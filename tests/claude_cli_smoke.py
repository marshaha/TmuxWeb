"""Run an installed Claude Code against a loopback model fixture in its own tmux server."""
import json
import os
from pathlib import Path
import shlex
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_chat
import chat_ipc


def main(binary):
    tool_results = []
    question = {"questions": [{"question": "Fixture colors?", "header": "Colors", "multiSelect": True,
                               "options": [{"label": c, "description": c} for c in ("Blue", "Green")]}]}

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?")[0] != "/v1/messages":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":8}')
                return
            answered = [part for m in request.get("messages", []) if isinstance(m["content"], list)
                        for part in m["content"] if part.get("tool_use_id") == "toolu_fixture"]
            tool_results.extend(answered)
            block = ({"type":"text","text":"fixture complete"} if answered else
                     {"type":"tool_use","name":"AskUserQuestion","id":"toolu_fixture","input":question})
            reason = "end_turn" if answered else "tool_use"
            message = {"id":"msg_fixture","type":"message","role":"assistant","model":request.get("model"),
                       "content":[block],"stop_reason":reason,"stop_sequence":None,
                       "usage":{"input_tokens":8,"output_tokens":8}}
            self.send_response(200)
            if not request.get("stream"):
                self.send_header("Content-Type","application/json")
                self.end_headers()
                self.wfile.write(json.dumps(message).encode())
                return
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            def event(name, **kw):
                body = json.dumps(dict(type=name, **kw))
                self.wfile.write(f"event: {name}\ndata: {body}\n\n".encode())
            event("message_start", message=dict(message,content=[],stop_reason=None))
            event("content_block_start", index=0, content_block=dict(block, **({"input":{}} if not answered else {"text":""})))
            delta = ({"type":"text_delta","text":"fixture complete"} if answered else
                     {"type":"input_json_delta","partial_json":json.dumps(question)})
            event("content_block_delta",index=0,delta=delta)
            event("content_block_stop",index=0)
            event("message_delta",delta={"stop_reason":reason,"stop_sequence":None}, usage={"output_tokens":8})
            event("message_stop")

    with tempfile.TemporaryDirectory(dir="/tmp",prefix="cchat-") as directory, ThreadingHTTPServer(("127.0.0.1",0),Model) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tmux = ["tmux", "-S", directory+"/tmux.sock", "-f", "/dev/null"]
        env = {"HOME":directory, "PATH":os.defpath, "ANTHROPIC_BASE_URL":f"http://127.0.0.1:{server.server_port}",
               "ANTHROPIC_API_KEY":"fixture", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1",
               "NO_PROXY":"127.0.0.1,localhost", "HTTPS_PROXY":"http://127.0.0.1:1",
               "HTTP_PROXY":"http://127.0.0.1:1", "TMPDIR":directory}
        launch = shlex.join([sys.executable, claude_chat.__file__, binary])
        with patch.dict(os.environ,env,clear=True):
            pane, pid = subprocess.check_output(tmux+["new-session","-d","-P","-F","#{pane_id} #{pane_pid}",
                                               "-c",directory,launch],text=True).split()
            def until(fn):
                end = time.monotonic()+35
                while time.monotonic() < end:
                    if result := fn():
                        return result
                    time.sleep(.08)
                screen = subprocess.run(tmux+["capture-pane","-p","-t",pane],capture_output=True,text=True)
                raise AssertionError("fixture timed out: " + screen.stdout.strip()[-1000:])
            def query(action=None):
                return chat_ipc.query(pid,pane,action,provider="claude")
            try:
                until(lambda: query({"binding": True}).get("session"))
                assert query({"prompt":"Ask the fixture question"})["status"] == "submitted"
                form = until(lambda: query().get("form"))
                assert form["questions"][0]["title"] == "Fixture colors?"
                def has_notice():
                    store = Path(directory)/".config/tmuxweb-agent/interactions.sqlite3"
                    if not store.exists():
                        return False
                    with closing(sqlite3.connect(store)) as db:
                        return db.execute("SELECT COUNT(*) FROM waits WHERE pane=? AND pid=? AND title!=''",
                                          (pane, pid)).fetchone()[0]
                until(has_notice)
                action = {"revision":form["revision"],"answers":{"0":{"indices":[0,1]}}}
                for _ in range(2):
                    assert query(action)["status"] == "resolved"
                until(lambda: tool_results)
                until(lambda: not has_notice())
                assert "Blue, Green" in json.dumps(tool_results) and not tool_results[0].get("is_error")
                print("PASS Claude Code CLI → AskUserQuestion → Chat → native tool_result; private tmux + loopback API")
            finally:
                subprocess.run(tmux+["kill-server"],capture_output=True)
                server.shutdown()


if __name__ == "__main__":
    main(sys.argv[1])
