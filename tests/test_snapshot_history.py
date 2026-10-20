import unittest

from tmux_web import SnapshotHistory


class SnapshotHistoryTests(unittest.TestCase):
    def test_appends_only_lines_after_overlapping_screen_content(self):
        history = SnapshotHistory(max_lines=100)
        first = history.update("%1", "A\nB\nC\n")
        second = history.update("%1", "B\nC\nD\nE\n")
        self.assertEqual(first, "A\nB\nC\n")
        self.assertEqual(second, "A\nB\nC\nD\nE\n")

    def test_repeated_screen_does_not_duplicate(self):
        history = SnapshotHistory(max_lines=100)
        history.update("%1", "A\nB\n")
        self.assertEqual(history.update("%1", "A\nB\n"), "A\nB\n")

    def test_unrelated_redraw_appends_snapshot_boundary(self):
        history = SnapshotHistory(max_lines=100)
        history.update("%1", "old one\nold two\n")
        result = history.update("%1", "new one\nnew two\n")
        self.assertIn("old two", result)
        self.assertIn("new one", result)

    def test_limits_accumulated_lines(self):
        history = SnapshotHistory(max_lines=4)
        history.update("%1", "1\n2\n3\n")
        result = history.update("%1", "3\n4\n5\n6\n")
        self.assertEqual(result.splitlines(), ["3", "4", "5", "6"])


if __name__ == "__main__":
    unittest.main()
