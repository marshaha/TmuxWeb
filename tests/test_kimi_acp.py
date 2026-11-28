import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import kimi_acp
import transcript_reader

QUESTIONS = {"sessionId":"session_fixture","mode":"form","message":"Select provider and models","requestedSchema":{
    "type":"object","required":["q0","q1"],"properties":{
        "q0":{"type":"string","title":"Provider","oneOf":[{"const":"p1","title":"One"},{"const":"p2","title":"Two"}]},
        "q1":{"type":"array","title":"Models","items":{"anyOf":[{"const":"m1"},{"const":"m2"}]}}}}}


class KimiForms(unittest.TestCase):
    def test_session_binding_and_atomic_multi_question_answers(self):
        owner = kimi_acp.Owner()
        with self.assertRaises(ValueError):
            owner.register(8,"elicitation/create",QUESTIONS)
        owner.session="session_fixture"
        owner.register(8,"elicitation/create",QUESTIONS)
        form=owner.handle(None,None)["form"]
        writes=[]
        action={"revision":form["revision"],"answers":{"q0":{"indices":[1]}}}
        self.assertEqual(owner.handle(action,writes.append)["status"],"invalid")
        action["answers"]["q1"]={"indices":[0,1]}
        self.assertEqual(owner.handle(dict(action,revision="x"),writes.append)["status"],"closed")
        for _ in range(2):
            self.assertEqual(owner.handle(action,writes.append)["status"],"resolved")
        self.assertEqual(writes,[{"jsonrpc":"2.0","id":8,"result":{"action":"accept","content":{"q0":"p2","q1":["m1","m2"]}}}])

    def test_same_directory_sessions_require_the_exact_id(self):
        with tempfile.TemporaryDirectory() as home:
            for name in ("session_a","session_b"):
                session=Path(home)/".kimi-code/sessions/wd_test"/name
                (session/"agents/main").mkdir(parents=True)
                (session/"agents/main/wire.jsonl").touch()
                (session/"state.json").write_text(json.dumps({"cwd":"/work","id":name,"title":"same"}))
            state,_=transcript_reader._kimi_session_path(home,"/work","same","session_a")
            self.assertEqual(state["id"],"session_a")
            with self.assertRaises(transcript_reader.TranscriptError):
                transcript_reader._kimi_session_path(home,"/work","same","session_missing")

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("tmux"),"requires Linux tmux")
    def test_private_tmux_with_stdio_peer(self):
        with tempfile.TemporaryDirectory(dir="/tmp",prefix="kacp-") as directory:
            root=Path(directory)
            sock=root/"tmux.sock"
            cmd=["tmux","-S",str(sock),"-f","/dev/null"]
            peer=Path(__file__).parent/"fixtures/kimi_acp_peer.py"
            launch=shlex.join([sys.executable,kimi_acp.__file__,str(peer.resolve())])
            with patch.dict(os.environ,{"HOME":directory}):
                pane,pid=subprocess.check_output(cmd+["new-session","-d","-P","-F","#{pane_id} #{pane_pid}",
                    "-c",directory,launch],text=True).split()
                try:
                    def until(predicate):
                        deadline=time.monotonic()+8
                        while time.monotonic()<deadline:
                            if result:=predicate():return result
                            time.sleep(.04)
                        self.fail("timed out waiting for isolated ACP peer")
                    until(lambda:kimi_acp.query(pid,pane).get("session"))
                    text="hello\nworld"
                    self.assertEqual(kimi_acp.query(pid,pane,{"prompt":text})["status"],"submitted")
                    form=until(lambda:kimi_acp.query(pid,pane).get("form"))
                    self.assertIn("echo fixture",form["questions"][0]["description"])
                    self.assertEqual(kimi_acp.query(pid,"%999")["status"],"closed")
                    self.assertEqual(kimi_acp.query(pid,pane,{"prompt":"another turn"})["status"],"busy")
                    action={"revision":form["revision"],"answers":{"permission":{"indices":[1]}}}
                    for _ in range(2):
                        self.assertEqual(kimi_acp.query(pid,pane,action)["status"],"resolved")
                    until(lambda:(root/"count").exists())
                    self.assertEqual((root/"count").read_text(),"1")
                    self.assertEqual(json.loads((root/"prompt.json").read_text())["prompt"][0]["text"],text)
                finally:
                    subprocess.run(cmd+["kill-server"],capture_output=True)
