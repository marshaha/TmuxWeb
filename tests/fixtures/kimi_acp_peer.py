#!/usr/bin/env python3
"""Offline ACP peer. Records only fixture messages in the test's temporary cwd."""
import json
import sys
from pathlib import Path

SID = "session_fixture"
def send(**msg):
    print(json.dumps(dict(jsonrpc="2.0", **msg)), flush=True)

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    ident = msg.get("id")
    if method == "initialize":
        assert msg["params"]["clientCapabilities"]["elicitation"] == {"form":{}}
        send(id=ident,result={"protocolVersion":1})
    elif method == "session/new":
        send(id=ident,result={"sessionId":SID})
    elif method == "session/prompt":
        Path("prompt.json").write_text(json.dumps(msg["params"]))
        turn = ident
        send(id=91,method="session/request_permission",params={
            "sessionId":SID,"options":[{"optionId":"once","kind":"allow_once","name":"Yes"},
                                      {"optionId":"no","kind":"reject_once","name":"No"}],
            "toolCall":{"toolCallId":"call","title":"Bash","content":[
                {"type":"content","content":{"type":"text","text":"echo fixture"}}]}})
    elif ident == 91:
        assert msg["result"]["outcome"] == {"outcome":"selected","optionId":"no"}
        Path("count").open("a").write("1")
        send(id=turn,result={"stopReason":"end_turn"})
