"""Web ⇄ Flutter parity gate, enforced by the server suite.

`scripts/parity.py` compares the routes/modules/pages the web console ships with
what the Flutter client actually calls or implements. This test makes it a hard
gate: adding an endpoint, a shared asset or a page without deciding what the app
does about it turns the suite red, and so does a manifest entry whose Dart file
was deleted.

Run the script directly for the readable report:
    python3 scripts/parity.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import parity  # noqa: E402


class ParityTests(unittest.TestCase):
    def test_the_manifest_has_no_drift(self):
        data = parity.report()
        problems = []
        for kind in ("api", "assets", "pages"):
            block = data[kind]
            for label in ("unlisted", "stale", "broken"):
                if block[label]:
                    problems.append(f"{kind}.{label}: {', '.join(block[label])}")
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertEqual(data["problems"], 0)

    def test_the_manifest_is_valid_json_with_known_sections(self):
        manifest = parity.load_manifest()
        allowed = {f"{state}_{kind}" for state in ("ported", "web_only", "pending")
                   for kind in ("api", "assets", "pages")}
        unknown = set(manifest) - allowed
        self.assertEqual(unknown, set(), f"parity.json 里有不认识的段: {sorted(unknown)}")

    def test_every_ported_entry_points_at_a_real_dart_file(self):
        manifest = parity.load_manifest()
        missing = []
        for kind in ("api", "assets", "pages"):
            for item, target in (manifest.get(f"ported_{kind}") or {}).items():
                if not parity.dart_file_exists(target):
                    missing.append(f"{item} -> {target}")
        self.assertEqual(missing, [], f"这些条目指向的 Dart 文件不存在: {missing}")

    def test_normalise_treats_both_placeholder_styles_the_same(self):
        self.assertEqual(parity.normalise("/api/panes/{pane_id}/files"),
                         parity.normalise("/api/panes/${paneRef}/files"))


if __name__ == "__main__":
    unittest.main()
