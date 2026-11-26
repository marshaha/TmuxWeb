from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.report_status import osc_state
from agent_calls import AgentCallStore
from home_store import HomeStore
from needs_you_inbox import NeedsYouInbox
from remote_hosts import parse_tmux_panes, script_report, FIELD_SEPARATOR


class ReportingTests(unittest.TestCase):
    def test_member_is_scoped_and_single_assignment_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HomeStore(Path(folder)/'home.db')
            store.assign_project('%4', '同项目')
            store.assign_project('fed~%4', '同项目')
            store.assign_project('%4', '本地')
            self.assertEqual(store.pane_projects(), {'%4':'本地', 'fed~%4':'同项目'})
            store.assign_project('fed~%4', '')
            self.assertEqual(store.pane_projects(), {'%4':'本地'})

    def test_osc_transport(self):
        for terminator in (b'\x07', b'\x1b\\'):
            self.assertEqual(osc_state(b'\x1b]7501;state=blocked:app=fixture'+terminator), 'blocked')
        for record in (b'state=approve', b'state=done:id=other', b'state=idle:state=error', b'?'):
            self.assertIsNone(osc_state(b'\x1b]7501;'+record+b'\x07'))

    @unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
    def test_hook_uses_only_its_private_socket_and_rejects_invalid_records(self):
        script = Path(__file__).resolve().parents[1] / "scripts/report_status.py"
        with tempfile.TemporaryDirectory() as folder:
            socket = str(Path(folder) / "fixture.sock")
            server = ["tmux", "-S", socket]
            env = dict(os.environ, TMUX=f"{socket},0,0")
            env.pop("TMUX_BIN", None)
            pane = subprocess.check_output(
                [*server, "-f", "/dev/null", "new-session", "-dP", "-F", "#{pane_id}", "sleep 60"],
                text=True).strip()
            env["TMUX_PANE"] = pane
            try:
                args = [sys.executable, str(script), "--osc"]
                option = [*server, "display-message", "-pt", pane, "#{@tmuxweb_status}"]
                subprocess.run(args, input=b"\x1b]7501;state=blocked\x07", env=env, check=True)
                signal = subprocess.check_output(option, text=True).strip()
                pid = subprocess.check_output([*server, "display-message", "-pt", pane, "#{pane_pid}"],
                                              text=True).strip()
                self.assertEqual(script_report(signal, pid)["state"], "blocked")
                self.assertNotEqual(subprocess.run(args, input=b"invalid", env=env,
                                                   capture_output=True).returncode, 0)
                self.assertEqual(subprocess.check_output(option, text=True).strip(), signal)
                subprocess.run([sys.executable, str(script), "clear"], env=env, check=True)
                self.assertEqual(subprocess.check_output(option, text=True).strip(), "")
            finally:
                subprocess.run([*server, "kill-server"], check=True)

    def test_script_reports_validate_generation_age_and_kind(self):
        stamp = int(time.time()*1000)
        raw = f'123:{stamp}:blocked'
        fields = ['%4','s:0.0','s','0','w','0','zsh','t','/tmp','1','80','24','0','123',raw]
        self.assertEqual(parse_tmux_panes(FIELD_SEPARATOR.join(fields),'aabbccddeeff0011')[0]['script_signal'],raw)
        for invalid in ('122:'+str(stamp)+':done', f'123:{stamp}:approve', '123:1000000000000:working'):
            self.assertIsNone(script_report(invalid,'123'))

    def test_done_survives_refresh_and_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            store = NeedsYouInbox(str(Path(folder)/'inbox.db'))
            now = time.time()
            def sweep(at):
                state={'id':'%4','pane_pid':'123','script_done_at':at,'provider':None}
                store.turn_completions([], [state])
                return state.get('completion_id')
            first = sweep(now)
            self.assertTrue(first)
            self.assertEqual(first,sweep(now))
            store.read_completion(first)
            self.assertIsNone(sweep(now))
            self.assertTrue(sweep(now+0.01))

    def test_old_call_database_and_timeout_keep_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'calls.db')
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,parent,child,question,reply,status,source,created,finished,error)')
            store = AgentCallStore(path)
            call = store.start('%1','remote~%1','hello',timeout=60)
            store.progress(call.id,'working')
            store.finish(call.id,status='timeout')
            receipt = store.outbound('%1')[0]
            self.assertEqual((receipt['status'],receipt['progress']),('timeout','working'))
            self.assertFalse(store.outbound('remote~%1'))
