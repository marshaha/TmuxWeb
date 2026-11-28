"""Exercise verified installs and rollbacks without a live node or a harness."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from plugins.agent import agent


class SkillSyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / "releases-root"
        self.dest = self.home / "profile" / "skills"
        self.hub = Mock()
        self.data = b"first"
        self.publish(self.data)

    def publish(self, data):
        self.hub.catalog.return_value = {"demo": {"SKILL.md": ("fixture", len(data), hashlib.sha256(data).hexdigest())}}
        self.hub.read_file.return_value = data

    def sync(self):
        return agent.install_skill_catalog(self.hub, self.root, {self.dest})

    def test_update_then_cached_rollback(self):
        first = self.sync()
        self.publish(b"second")
        second = self.sync()
        self.assertNotEqual(first["version"], second["version"])
        inode = (self.root / "current").lstat().st_ino
        self.sync()
        self.assertEqual(inode, (self.root / "current").lstat().st_ino)
        self.publish(self.data)
        self.assertEqual(self.sync()["version"], first["version"])
        self.assertEqual(self.hub.read_file.call_count, 2)
        self.assertEqual((self.dest / "demo/SKILL.md").read_bytes(), self.data)

    def test_partial_download_preserves_active_skill(self):
        self.sync()
        self.publish(b"new")
        self.hub.read_file.return_value = b"bad"
        with self.assertRaisesRegex(ValueError, "digest"):
            self.sync()
        self.assertEqual((self.dest / "demo/SKILL.md").read_bytes(), self.data)

    def test_preserve_personal_skill_and_remove_only_our_link(self):
        self.dest.mkdir(parents=True)
        personal = self.dest / "demo"
        personal.mkdir()
        with self.assertRaisesRegex(ValueError, "personal"):
            self.sync()
        personal.rename(self.dest / "mine")
        self.sync()
        self.hub.catalog.return_value = {}
        self.sync()
        self.assertFalse(os.path.lexists(personal))
        self.assertTrue((self.dest / "mine").is_dir())

    def test_no_extra_snapshot_entries(self):
        self.sync()
        (self.dest / "demo/extra").write_text("modified")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.sync()

    def test_catalog_rejects_aliases_and_traversal(self):
        h = object.__new__(agent.SkillHub)
        h.allow_empty = False
        prefix = "skill://example/id/demo/"
        for path in ["%2e%2e/a", "skill.md", "docs", "Docs/b", "d%00oc"]:
            with self.subTest(path=path):
                names = ["SKILL.md", "docs/a", path]
                entry = {"uri": prefix+"SKILL.md", "frontmatter": {"name": "demo"},
                         "resources": [{"uri": prefix+n, "size": 1, "digest": "sha256:"+"0"*64} for n in names]}
                with patch.object(h, "rpc", side_effect=[{"capabilities":{"extensions":{agent.SKILL_EXTENSION:{}}}},
                                                         {"skills":[entry],"resultType":"complete"}]):
                    with self.assertRaises(ValueError):
                        h.catalog()

    def test_empty_group_requires_opt_in(self):
        h = object.__new__(agent.SkillHub)
        for allow in (True, False):
            h.allow_empty = allow
            with patch.object(h, "rpc", side_effect=[{"capabilities":{"extensions":{agent.SKILL_EXTENSION:{}}}},
                                                     {"skills":[],"resultType":"complete"}]):
                if allow:
                    self.assertEqual(h.catalog(), {})
                else:
                    with self.assertRaisesRegex(ValueError, "inaccessible"):
                        h.catalog()

    def test_only_typed_nonsecret_heartbeat_fields_are_recorded(self):
        from plugins.agent.transport import AgentRegistry
        registry = AgentRegistry()
        registry.note_skill_sync("h", {"ok":False,"count":False,"version":123, "checked_at":4,
                                       "error":"sensitive operator detail"})
        self.assertEqual(registry.status("h")["skill_sync"], {"ok":False,"checked_at":4})
        registry.note_skill_sync("h", None)
        self.assertNotIn("skill_sync",registry.status("h"))
