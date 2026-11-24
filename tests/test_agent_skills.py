import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_skills import catalog


class SkillCatalogTests(unittest.TestCase):
    def test_project_scope_dedup_and_private_manifests(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp, "CODEX_HOME": tmp + "/codex"}):
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            cwd = root / "src"
            cwd.mkdir()
            for folder in (root / ".agents/skills/demo", Path(tmp) / "codex/skills/.system/demo"):
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text("---\nname: demo\ndescription: test\n---\nprivate body")
            hidden = root / ".agents/skills/hidden"
            hidden.mkdir()
            (hidden / "SKILL.md").write_text("---\nname: hidden\nuser-invocable: false # private\n---\n")
            (hidden.parent / "linked").mkdir()
            (hidden.parent / "linked/SKILL.md").symlink_to(hidden / "SKILL.md")
            self.assertEqual(catalog("codex", str(cwd)), ["demo"])
            self.assertEqual(catalog("claude", str(cwd)), [])

    def test_nested_hermes_and_flat_kimi_manifests(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp, "HERMES_HOME": tmp, "KIMI_CODE_HOME": tmp+"/kimi"}):
            folder = Path(tmp) / "skills/writing/edit"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("---\nname: copy-edit\n---\n")
            skills = Path(tmp) / "kimi/skills"
            skills.mkdir(parents=True)
            (skills / "review.md").write_text("Review a change")
            self.assertEqual(catalog("hermes", ""), ["copy-edit"])
            self.assertEqual(catalog("kimi", ""), ["review"])
