import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import file_browser
from file_browser import FileBrowserError, tar_to_zip, zip_paths


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "zeta.txt").write_text("hello")
        (self.root / "alpha.txt").write_text("1234567890")
        (self.root / ".hidden").write_text("x")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "inner.txt").write_text("inner")
        try:
            (self.root / "link.txt").symlink_to(self.root / "alpha.txt")
        except OSError:
            pass

    def tearDown(self):
        self.temporary.cleanup()

    def test_directories_come_first_and_hidden_files_are_filtered(self):
        listing = file_browser.list_directory(self.root)
        names = [entry["name"] for entry in listing["entries"]]
        self.assertEqual(names[0], "sub")
        self.assertNotIn(".hidden", names)
        self.assertIn("alpha.txt", names)
        self.assertEqual(listing["path"], os.path.realpath(self.root))
        self.assertEqual(listing["parent"], os.path.realpath(self.root.parent))
        self.assertFalse(listing["truncated"])

    def test_hidden_files_and_sizes_are_reported(self):
        listing = file_browser.list_directory(self.root, show_hidden=True)
        by_name = {entry["name"]: entry for entry in listing["entries"]}
        self.assertIn(".hidden", by_name)
        self.assertEqual(by_name["alpha.txt"]["size"], 10)
        self.assertEqual(by_name["alpha.txt"]["type"], "file")
        self.assertEqual(by_name["sub"]["type"], "dir")
        self.assertEqual(len(by_name["alpha.txt"]["mode"]), 4)
        self.assertGreater(by_name["alpha.txt"]["mtime"], 0)

    def test_entry_limit_marks_the_listing_truncated(self):
        listing = file_browser.list_directory(self.root, limit=1)
        self.assertEqual(len(listing["entries"]), 1)
        self.assertTrue(listing["truncated"])

    def test_errors_are_explicit(self):
        with self.assertRaises(file_browser.FileBrowserError):
            file_browser.list_directory(self.root / "missing")
        with self.assertRaises(file_browser.FileBrowserError):
            file_browser.list_directory(self.root / "alpha.txt")
        with self.assertRaises(file_browser.FileBrowserError):
            file_browser.check_path("bad\nname")


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_files_and_directories_are_zipped_relative_to_the_base(self):
        Path(self.directory, "top.txt").write_text("top")
        (Path(self.directory) / "src").mkdir()
        Path(self.directory, "src", "main.py").write_text("print(1)")
        (Path(self.directory) / "empty").mkdir()
        blob = zip_paths(["top.txt", "src", "empty"], self.directory)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["empty/", "src/", "src/main.py", "top.txt"])
            self.assertEqual(archive.read("src/main.py"), b"print(1)")

    def test_a_selected_directory_absorbs_its_children(self):
        (Path(self.directory) / "pkg").mkdir()
        Path(self.directory, "pkg", "a.txt").write_text("a")
        blob = zip_paths(["pkg", "pkg/a.txt"], self.directory)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(archive.namelist().count("pkg/a.txt"), 1)

    def test_paths_outside_the_base_use_their_own_name(self):
        blob = zip_paths(["/etc/hostname"], "/tmp")
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(archive.namelist(), ["hostname"])

    def test_limits_and_empty_selections_are_reported(self):
        Path(self.directory, "big.bin").write_bytes(b"x" * 2048)
        with self.assertRaises(FileBrowserError):
            zip_paths(["big.bin"], self.directory, max_bytes=1024)
        with self.assertRaises(FileBrowserError):
            zip_paths(["big.bin"], self.directory, max_bytes=10 ** 6, max_files=0)
        with self.assertRaises(FileBrowserError):
            zip_paths([], self.directory)
        with self.assertRaises(FileBrowserError):
            zip_paths(["missing.txt"], self.directory)

    def test_tar_stream_from_a_remote_host_becomes_a_zip(self):
        buffer = io.BytesIO()
        payload = Path(self.directory, "payload.txt")
        payload.write_text("remote payload")
        (Path(self.directory) / "nested").mkdir()
        Path(self.directory, "nested", "inner.txt").write_text("inner")
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.add(payload, arcname="payload.txt")
            archive.add(Path(self.directory) / "nested", arcname="nested")
        blob = tar_to_zip(buffer.getvalue())
        with zipfile.ZipFile(io.BytesIO(blob)) as zipped:
            self.assertEqual(sorted(name for name in zipped.namelist() if not name.endswith("/")),
                             ["nested/inner.txt", "payload.txt"])
            self.assertEqual(zipped.read("payload.txt"), b"remote payload")

    def test_garbage_tar_payload_is_rejected(self):
        with self.assertRaises(FileBrowserError):
            tar_to_zip(b"not a tar stream at all")


class ResolveTests(unittest.TestCase):
    def test_relative_paths_resolve_against_the_pane_directory(self):
        self.assertEqual(file_browser.resolve_path("sub/inner.txt", "/srv/work"),
                         Path("/srv/work/sub/inner.txt"))
        self.assertEqual(file_browser.resolve_path("/etc/hosts"), Path("/etc/hosts"))
        self.assertEqual(file_browser.resolve_path("", "/srv/work"), Path("/srv/work"))

    def test_control_characters_are_rejected(self):
        for value in ["a\x00b", "a\nb", "a\rb"]:
            with self.assertRaises(file_browser.FileBrowserError):
                file_browser.resolve_path(value)

    def test_relative_path_without_base_is_rejected(self):
        with self.assertRaises(file_browser.FileBrowserError):
            file_browser.resolve_path("relative/path")


class CliTests(unittest.TestCase):
    def test_cli_prints_json_for_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "file.txt").write_text("x")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = file_browser.main(["--list", directory])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entries"][0]["name"], "file.txt")

    def test_cli_reports_errors_as_json(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = file_browser.main(["--list", "/definitely/not/here"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("does not exist", payload["error"])


if __name__ == "__main__":
    unittest.main()
