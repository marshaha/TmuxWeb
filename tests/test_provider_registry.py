"""The providers/ registry is the single source of truth — and the
three consumers that cannot import it are pinned to it here.

* The server (agent_status, needs_input, agent_catalog, agent_models)
  imports the registry directly.
* The agent program (plugins/agent/agent.py) ships as one file, so
  its provider facts are a generated block (see
  scripts/generate_agent_provider_facts.py). DriftTests fails if the
  embedded block differs from what the generator renders, and if the
  agent's verdicts disagree with the server's on the same files.
* transcript_reader.py is piped over SSH to `python3 -`, so it must
  stay a single shippable unit and keeps its own copies of detection
  and session resolution. DriftTests fails if those copies disagree
  with the registry on the fixture corpus below.

All fixtures use disposable temp homes and fake session files; no
live pane or host is touched.
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_catalog
import agent_models
import agent_status
import needs_input
import providers
import transcript_reader
from generate_agent_provider_facts import COPIED_FUNCTIONS, render_block
from generate_agent_provider_facts import extract_block

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "plugins" / "agent"))
import agent as agent_program


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                    encoding="utf-8")
    return path


def _claude_assistant(tool_id="toolu_1"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash"}]}}


def _claude_result(tool_id="toolu_1"):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


class RegistryShapeTests(unittest.TestCase):
    def test_four_providers_in_registry_order(self):
        self.assertEqual(providers.names(), ("claude", "codex", "kimi", "hermes"))
        self.assertEqual([spec.name for spec in providers.all_specs()],
                         ["claude", "codex", "kimi", "hermes"])

    def test_get_is_case_insensitive_and_none_for_unknown(self):
        self.assertIs(providers.get("Claude"), providers.get("claude"))
        self.assertIsNone(providers.get("shell"))
        self.assertIsNone(providers.get(""))

    def test_gemini_and_opencode_are_not_providers(self):
        """Catalog-only, honored by no other layer — see providers/README.md."""
        self.assertNotIn("gemini", providers.SPECS)
        self.assertNotIn("opencode", providers.SPECS)
        self.assertIsNone(providers.get("gemini"))
        self.assertIsNone(providers.get("opencode"))

    def test_gating_is_a_registry_property(self):
        self.assertEqual(providers.gated_providers(), ("claude", "codex", "kimi"))
        self.assertTrue(providers.get("claude").gated)
        self.assertTrue(providers.get("codex").gated)
        self.assertTrue(providers.get("kimi").gated)
        # hermes has no decision signal in its session file: opted out.
        self.assertFalse(providers.get("hermes").gated)
        self.assertEqual(needs_input.GATED_PROVIDERS, providers.gated_providers())

    def test_markers_are_exactly_as_before(self):
        """The literal substrings agent_status used before the registry."""
        expected = {
            "claude": (('"subtype":"turn_duration"', '"subtype":"stop_hook_summary"'),
                       ('"type":"user"', '"type":"assistant"')),
            "codex": (('"type":"task_complete"', '"type":"turn_aborted"'),
                      ('"type":"task_started"', '"type":"item_completed"',
                       '"type":"token_count"')),
            "kimi": (('"type":"prompt.completed"', '"type":"turn.ended"'),
                     ('"type":"prompt.submitted"', '"type":"llm.request"',
                      '"type":"llm.response"', '"type":"step.start"',
                      '"type":"step.end"', '"type":"tool.call"',
                      '"type":"agent.message.appended"')),
            "hermes": ((), ()),
        }
        for name, (idle, busy) in expected.items():
            spec = providers.get(name)
            self.assertEqual(spec.idle_markers, idle, name)
            self.assertEqual(spec.busy_markers, busy, name)
            self.assertEqual(agent_status.IDLE_MARKERS[name], idle, name)
            self.assertEqual(agent_status.BUSY_MARKERS[name], busy, name)

    def test_catalog_and_models_read_the_registry(self):
        for name in ("hermes", "claude", "codex", "kimi"):
            catalog = agent_catalog.spec_for(name)
            spec = providers.get(name)
            self.assertEqual(catalog["bins"], spec.bins, name)
            self.assertEqual(catalog["label"], spec.label, name)
            self.assertEqual(agent_models.preset_for(name), list(spec.model_presets), name)
            self.assertEqual(agent_models.PROVIDER_LABELS[name], spec.provider_label, name)
        self.assertEqual(agent_models.SWITCH_TEMPLATES["kimi"], "/model")
        self.assertTrue(agent_models.picker_only("kimi"))
        self.assertFalse(agent_models.picker_only("claude"))
        # opencode/gemini stay catalog-only: bins, and nothing else.
        self.assertEqual(agent_catalog.spec_for("opencode")["bins"], ("opencode",))
        self.assertEqual(agent_catalog.spec_for("gemini")["bins"], ("gemini",))
        with self.assertRaises(ValueError):
            agent_models.switch_command("gemini", "x")

    def test_resume_argv_is_interface_only(self):
        """Flags only — no restart feature in this PR (PR #23 plugs in here)."""
        self.assertEqual(providers.get("claude").resume_argv("sess-1"),
                         ["--resume", "sess-1"])
        self.assertEqual(providers.get("codex").resume_argv("thread-1"),
                         ["resume", "thread-1"])
        self.assertEqual(providers.get("kimi").resume_argv("sess-1"),
                         ["--resume", "sess-1"])
        self.assertEqual(providers.get("hermes").resume_argv("sess-1"),
                         ["--resume", "sess-1"])

    def test_hook_templates_match_the_files_on_disk(self):
        hooks_dir = Path(__file__).resolve().parent.parent / "plugins" / "agent" / "hooks"
        for name in ("claude", "codex", "kimi"):
            spec = providers.get(name)
            self.assertTrue(spec.hook_template)
            self.assertTrue((hooks_dir / spec.hook_template).is_file(), name)
        self.assertEqual(providers.get("hermes").hook_template, "")

    def test_hook_event_mapping_is_exactly_as_before(self):
        expected = {
            "claude": (("permission_prompt", "elicitation_dialog"), ("stop",)),
            "codex": (("PermissionRequest",), ("Stop",)),
            "kimi": (("PermissionRequest",), ("PermissionResult", "Stop")),
            "hermes": ((), ()),
        }
        for name, (waiting, clearing) in expected.items():
            spec = providers.get(name)
            self.assertEqual(spec.hook_waiting, waiting, name)
            self.assertEqual(spec.hook_clearing, clearing, name)
        # The agent's generic sets are the registry union plus the
        # documented generic extras (no provider emits those).
        normalise = agent_program._normalise_hook_event
        waiting = {normalise(n) for spec in providers.all_specs()
                   for n in spec.hook_waiting}
        clearing = {normalise(n) for spec in providers.all_specs()
                    for n in spec.hook_clearing}
        self.assertEqual(agent_program._HOOK_WAITING, waiting | {"agentneedsinput"})
        self.assertEqual(agent_program._HOOK_CLEARING,
                         clearing | {"sessionend", "interrupt",
                                     "userpromptsubmit", "turnstarted"})


class DetectionTests(unittest.TestCase):
    COMMANDS = [
        ("claude", "claude"), ("claude-code", "claude"),
        ("/usr/local/bin/claude --foo", None),
        ("Claude", "claude"),
        ("codex", "codex"), ("codex resume abc-123", "codex"),
        ("kimi", "kimi"), ("kimi-code", "kimi"),
        ("hermes", "hermes"), ("hermes-agent", "hermes"),
        ("python3", "hermes"), ("python", "hermes"),
        ("python3 -m hermes", "hermes"),
        ("zsh", None), ("bash", None), ("node", None),
        ("", None), (None, None),
    ]
    ARGVS = [
        ("/opt/homebrew/bin/codex --flag", "codex"),
        ("node /x/@openai/codex/bin/codex.js", "codex"),
        ("kimi-code", "kimi"),
        ("/usr/bin/hermes-agent", "hermes"),
        ("python3 /path/to/hermes-agent.py", "hermes"),
        ("claude --resume abc", "claude"),
        ("zsh -c 'codex'", None),
        ("vim file.txt", None),
        ("", None),
    ]

    def test_provider_for_command_matches_the_reader(self):
        for command, expected in self.COMMANDS:
            self.assertEqual(providers.provider_for_command(command), expected, command)
            self.assertEqual(transcript_reader.provider_for_command(command),
                             expected, command)

    def test_provider_from_argv_matches_the_reader(self):
        for argv, expected in self.ARGVS:
            self.assertEqual(providers.provider_from_argv(argv), expected, argv)
            self.assertEqual(transcript_reader.provider_from_argv(argv), expected, argv)


class OutstandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _records_file(self, records):
        """A temp jsonl holding ``records`` (for agent_status file APIs)."""
        path = Path(self.tmp.name) / "records.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                        encoding="utf-8")
        return path

    def _check(self, provider, records, expected):
        self.assertEqual(bool(providers.get(provider).outstanding(records)),
                         expected, (provider, records))
        self.assertEqual(agent_status.has_outstanding_call(
            provider, self._records_file(records)), expected)
        self.assertEqual(agent_program._watcher_outstanding(provider, records),
                         expected, (provider, records))

    def test_claude_pending_completed_and_abandoned(self):
        self._check("claude", [{"type": "user"}, _claude_assistant("t1")], True)
        self._check("claude", [{"type": "user"}, _claude_assistant("t1"),
                            _claude_result("t1")], False)
        # two parallel calls need every result back
        self._check("claude", [{"type": "user"}, _claude_assistant("t1"),
                            _claude_assistant("t2"), _claude_result("t1")], True)
        # a plain user message after the call abandons it
        self._check("claude", [{"type": "user"}, _claude_assistant("t1"),
                            {"type": "user", "message": {"content": "go on"}}], False)
        # a turn-end marker closes everything before it
        self._check("claude", [{"type": "user"}, _claude_assistant("t1"),
                            {"type": "system", "subtype": "turn_duration"}], False)

    def test_codex_call_needs_its_output(self):
        call = {"type": "response_item", "payload": {"type": "function_call",
                                                     "call_id": "c1"}}
        out = {"type": "response_item", "payload": {"type": "function_call_output",
                                                    "call_id": "c1"}}
        self._check("codex", [call], True)
        self._check("codex", [call, out], False)
        self._check("codex", [call, {"type": "event_msg",
                                     "payload": {"type": "task_complete"}}], False)

    def test_kimi_tool_call_closes_on_step_end_or_llm_request(self):
        self._check("kimi", [{"type": "llm.request"}, {"type": "tool.call"}], True)
        self._check("kimi", [{"type": "tool.call"}, {"type": "step.end"}], False)
        self._check("kimi", [{"type": "tool.call"}, {"type": "llm.request"}], False)
        self._check("kimi", [{"type": "tool.call"},
                             {"type": "context.append_loop_event",
                              "event": {"type": "step.end"}}], False)

    def test_hermes_never_outstanding(self):
        self.assertFalse(providers.get("hermes").outstanding([{"type": "user"}]))
        self.assertFalse(agent_program._watcher_outstanding("hermes", [{"type": "user"}]))


class SessionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _claude_project(self, name="sess-1", records=(), cwd="/proj"):
        project = self.home / ".claude" / "projects" / "-proj"
        return _write_jsonl(project / (name + ".jsonl"), list(records))

    def test_claude_hint_title_and_newest(self):
        old = self._claude_project("old", [{"type": "user"}])
        new = self._claude_project("new", [{"type": "user"}, _claude_assistant()])
        os.utime(old, (1000000000, 1000000000))
        os.utime(new, (1700000000, 1700000000))
        spec = providers.get("claude")
        # hint wins outright
        self.assertEqual(spec.resolve_session_file(self.home, "/proj", "", hint="old"), old)
        # title wins when its bytes match exactly one file
        titled = self._claude_project("titled", [{"type": "user", "message": {
            "content": "unique-title-xyz"}}])
        os.utime(titled, (1200000000, 1200000000))  # older than `new`
        self.assertEqual(
            spec.resolve_session_file(self.home, "/proj", "unique-title-xyz"), titled)
        # nothing attributable: the newest file (the old agent_status fallback)
        self.assertEqual(spec.resolve_session_file(self.home, "/proj", ""), new)
        self.assertEqual(spec.resolve_session_file(self.home, "/elsewhere", ""), None)

    def test_codex_hint_title_and_session_meta(self):
        root = self.home / ".codex" / "sessions" / "2026"
        first = _write_jsonl(root / "rollout-1.jsonl", [
            {"type": "session_meta", "payload": {"cwd": "/proj", "id": "thread-1"}},
            {"type": "response_item", "payload": {"type": "function_call",
                                                  "call_id": "c1"}}])
        _write_jsonl(root / "rollout-2.jsonl", [
            {"type": "session_meta", "payload": {"cwd": "/other", "id": "thread-2"}}])
        (self.home / ".codex" / "session_index.jsonl").write_text(
            json.dumps({"id": "thread-1", "thread_name": "my task"}) + "\n",
            encoding="utf-8")
        spec = providers.get("codex")
        self.assertEqual(spec.resolve_session_file(self.home, "/proj", ""), first)
        self.assertEqual(spec.resolve_session_file(self.home, "/proj", "my task"), first)
        self.assertEqual(
            spec.resolve_session_file(self.home, "/proj", "", hint="rollout-1"), first)
        self.assertEqual(spec.resolve_session_file(self.home, "/nowhere", ""), None)

    def test_kimi_title_preferred_newest_wins(self):
        def session(name, cwd="/proj", title="t", stamp=1780000000000):
            state_dir = (self.home / ".kimi-code" / "sessions" / "wd_abc"
                         / ("session_" + name))
            wire = state_dir / "agents" / "main" / "wire.jsonl"
            _write_jsonl(wire, [{"type": "llm.request"}])
            (state_dir / "state.json").write_text(json.dumps(
                {"cwd": cwd, "title": title, "updatedAt": stamp}), encoding="utf-8")
            return wire
        session("old", stamp=1780000000000)
        newer = session("new", stamp=1780000001000)
        spec = providers.get("kimi")
        self.assertEqual(spec.resolve_session_file(self.home, "/proj", ""), newer)
        self.assertEqual(spec.resolve_session_file(self.home, "/proj", "t"), newer)
        self.assertEqual(spec.resolve_session_file(self.home, "/elsewhere", ""), None)

    def test_hermes_has_no_session_file(self):
        spec = providers.get("hermes")
        self.assertIsNone(spec.resolve_session_file(self.home, "/proj", ""))
        self.assertIsNone(agent_status._session_path("hermes", "/proj", home=self.home))
        self.assertEqual(agent_status.status_for_pane("hermes", "/proj", home=self.home),
                         agent_status.UNKNOWN)


class TranscriptSourceTests(unittest.TestCase):
    def test_each_provider_source_validates(self):
        for spec in providers.all_specs():
            providers.validate_transcript_sources(spec.transcript_source)
        kinds = {spec.name: [s["kind"] for s in spec.transcript_source]
                 for spec in providers.all_specs()}
        self.assertEqual(kinds["claude"], ["jsonl"])
        self.assertEqual(kinds["codex"], ["jsonl"])
        self.assertEqual(kinds["kimi"], ["jsonl"])
        self.assertEqual(kinds["hermes"], ["sqlite", "jsonl"])

    def test_the_slot_expresses_opencode_without_adding_it(self):
        """OpenCode's shape (sqlite + CLI export) fits the interface, so
        a future provider module can use it — no implementation here."""
        providers.validate_transcript_sources((
            {"kind": "sqlite", "db": "~/.local/share/opencode/opencode.db",
             "tables": {"session": "id, project, ...",
                        "message": "id, session_id, role, ...",
                        "part": "id, message_id, type, data"}},
            {"kind": "cli", "commands": ["opencode export <session_id>",
                                         "opencode session list --format json",
                                         "opencode db path"]},
        ))

    def test_the_slot_expresses_a_file_path_shape(self):
        """The shape Gemini would need (verified layout, not a provider)."""
        providers.validate_transcript_sources((
            {"kind": "jsonl", "root": "~/.gemini/tmp",
             "layout": "*/chats/session-*.jsonl"},
        ))

    def test_invalid_sources_are_rejected(self):
        for bad in ((), [], [{"kind": "xml", "root": "~"}],
                    [{"kind": "jsonl", "root": "~"}],
                    [{"kind": "sqlite", "db": "~/.x.db"}],
                    [{"kind": "cli", "commands": []}],
                    ["not-a-dict"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                providers.validate_transcript_sources(bad)


class DriftTests(unittest.TestCase):
    """Server copy vs agent copy vs piped-reader copy must agree."""

    def test_agent_block_matches_the_generator(self):
        agent_path = (Path(__file__).resolve().parent.parent
                      / "plugins" / "agent" / "agent.py")
        embedded = extract_block(agent_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(embedded, "agent.py lost its generated block")
        self.assertEqual(embedded, render_block().rstrip("\n"))

    def test_copied_functions_use_no_new_syntax(self):
        """They run on hosts with older interpreters (macmini: py3.9):
        no match statements, no `X | Y` unions."""
        import providers as _providers
        modules = {"claude": _providers.claude, "codex": _providers.codex,
                   "kimi": _providers.kimi, "hermes": _providers.hermes}
        import inspect
        for name, func_names in COPIED_FUNCTIONS.items():
            for func_name in func_names:
                tree = ast.parse(inspect.getsource(getattr(modules[name], func_name)))
                for node in ast.walk(tree):
                    self.assertNotIsInstance(node, ast.Match, func_name)
                    if isinstance(node, ast.BinOp):
                        self.assertIsNot(node.op, ast.BitOr, func_name)

    def test_agent_and_server_agree_on_decision_points(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cases = [
                ("claude", [{"type": "user"}, _claude_assistant()], True),
                ("claude", [{"type": "user"}, _claude_assistant(),
                            _claude_result()], False),
                ("claude", [{"type": "user"},
                            {"type": "system", "subtype": "turn_duration"}], True),
                ("codex", [{"type": "response_item", "payload": {
                    "type": "function_call", "call_id": "c1"}}], True),
                ("codex", [{"type": "event_msg",
                            "payload": {"type": "task_complete"}}], True),
                ("kimi", [{"type": "tool.call"}], True),
                ("kimi", [{"type": "tool.call"}, {"type": "step.end"}], False),
                ("hermes", [{"type": "user"}], False),
            ]
            for provider, records, expected in cases:
                path = _write_jsonl(Path(tmp.name) / (provider + ".jsonl"), records)
                self.assertEqual(agent_status.decision_point_for_file(provider, path),
                                 expected, (provider, records))
                self.assertEqual(agent_program._watcher_decision_point(provider, path),
                                 expected, (provider, records))
        finally:
            tmp.cleanup()

    def test_agent_and_server_agree_on_session_resolution(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            home = Path(tmp.name) / "home"
            project = home / ".claude" / "projects" / "-proj"
            old = _write_jsonl(project / "old.jsonl", [{"type": "user"}])
            new = _write_jsonl(project / "new.jsonl",
                               [{"type": "user"}, _claude_assistant()])
            os.utime(old, (1000000000, 1000000000))
            os.utime(new, (1700000000, 1700000000))
            self.assertEqual(
                agent_program._watcher_resolve_session_file("claude", "/proj",
                                                            home=home), new)
            self.assertEqual(agent_status._session_path("claude", "/proj", home=home),
                             new)
            self.assertEqual(
                agent_program._watcher_resolve_session_file(
                    "claude", "/proj", hint="old", home=home), old)
            # Kimi: the watcher used to ignore the title; it now matches
            # the server (title-preferred, newest wins).
            state_dir = home / ".kimi-code" / "sessions" / "wd_abc" / "session_xyz"
            wire = state_dir / "agents" / "main" / "wire.jsonl"
            _write_jsonl(wire, [{"type": "llm.request"}])
            (state_dir / "state.json").write_text(json.dumps(
                {"cwd": "/proj", "title": "t", "updatedAt": 1780000000000}),
                encoding="utf-8")
            self.assertEqual(
                agent_program._watcher_resolve_session_file(
                    "kimi", "/proj", title="t", home=home), wire)
            self.assertEqual(agent_status._session_path("kimi", "/proj", title="t",
                                                        home=home), wire)
        finally:
            tmp.cleanup()

    def test_reader_detection_matches_the_registry(self):
        for command, _ in DetectionTests.COMMANDS:
            self.assertEqual(transcript_reader.provider_for_command(command),
                             providers.provider_for_command(command), command)
        for argv, _ in DetectionTests.ARGVS:
            self.assertEqual(transcript_reader.provider_from_argv(argv),
                             providers.provider_from_argv(argv), argv)

    def test_reader_session_paths_match_the_registry(self):
        """The piped file's copies resolve the same sessions. Where the
        reader raises (nothing attributable), the registry returns the
        newest file — the old agent_status fallback — instead."""
        tmp = tempfile.TemporaryDirectory()
        try:
            home = Path(tmp.name) / "home"
            project = home / ".claude" / "projects" / "-proj"
            old = _write_jsonl(project / "old.jsonl", [{"type": "user"}])
            new = _write_jsonl(project / "new.jsonl",
                               [{"type": "user"}, _claude_assistant()])
            os.utime(old, (1000000000, 1000000000))
            os.utime(new, (1700000000, 1700000000))
            spec = providers.get("claude")
            self.assertEqual(spec.resolve_session_file(home, "/proj", "", hint="old"),
                             transcript_reader._claude_session_path(
                                 home, "/proj", "", None, "old"))
            titled = _write_jsonl(project / "titled.jsonl", [
                {"type": "user", "message": {"content": "unique-title-xyz"}}])
            os.utime(titled, (1200000000, 1200000000))  # older than `new`
            self.assertEqual(
                spec.resolve_session_file(home, "/proj", "unique-title-xyz"),
                transcript_reader._claude_session_path(
                    home, "/proj", "unique-title-xyz", None, None))
            self.assertEqual(
                transcript_reader._claude_session_path(
                    home, "/proj", "unique-title-xyz", None, None), titled)
            with self.assertRaises(transcript_reader.TranscriptError):
                transcript_reader._claude_session_path(home, "/proj", "", None, None)
            self.assertEqual(spec.resolve_session_file(home, "/proj", ""), new)

            root = home / ".codex" / "sessions" / "2026"
            first = _write_jsonl(root / "rollout-1.jsonl", [
                {"type": "session_meta",
                 "payload": {"cwd": "/proj", "id": "thread-1"}}])
            _, reader_path = transcript_reader._codex_session_path(
                home, "/proj", "")
            self.assertEqual(providers.get("codex").resolve_session_file(
                home, "/proj", ""), reader_path)
            self.assertEqual(reader_path, first)

            state_dir = home / ".kimi-code" / "sessions" / "wd_abc" / "session_xyz"
            wire = state_dir / "agents" / "main" / "wire.jsonl"
            _write_jsonl(wire, [{"type": "llm.request"}])
            (state_dir / "state.json").write_text(json.dumps(
                {"cwd": "/proj", "title": "t", "updatedAt": 1780000000000}),
                encoding="utf-8")
            _, reader_wire = transcript_reader._kimi_session_path(home, "/proj", "t")
            self.assertEqual(providers.get("kimi").resolve_session_file(
                home, "/proj", "t"), reader_wire)
            self.assertEqual(reader_wire, wire)
        finally:
            tmp.cleanup()

    def test_shell_status_uses_the_registry_markers(self):
        """STATUS_SHELL is built from the registry: every marker must
        appear in it (the shell-agreement tests prove the behaviour)."""
        for spec in providers.all_specs():
            for marker in list(spec.idle_markers) + list(spec.busy_markers):
                self.assertIn(marker, agent_status.STATUS_SHELL, spec.name)


if __name__ == "__main__":
    unittest.main()
