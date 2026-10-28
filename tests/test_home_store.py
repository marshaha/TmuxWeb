import tempfile
import unittest
from pathlib import Path

from home_store import HomeStore


class HomeStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = HomeStore(Path(self.directory.name) / "home.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_favourites_round_trip(self):
        self.assertEqual(self.store.favorites(), [])
        self.store.add_favorite("%7", "watch this")
        self.assertTrue(self.store.is_favorite("%7"))
        self.assertEqual(self.store.favorites()[0]["note"], "watch this")
        # adding twice keeps one row and updates the note
        self.store.add_favorite("%7", "renamed")
        rows = self.store.favorites()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "renamed")
        self.assertTrue(self.store.remove_favorite("%7"))
        self.assertFalse(self.store.remove_favorite("%7"))
        self.assertEqual(self.store.favorites(), [])

    def test_favourites_reject_an_empty_pane(self):
        with self.assertRaises(ValueError):
            self.store.add_favorite("   ")

    def test_cards_round_trip_and_kind_defaults(self):
        card = self.store.add_card("构建完成", "apk 已出包", "%7", "result")
        self.assertEqual(card["kind"], "result")
        self.assertEqual(self.store.card(card["id"])["title"], "构建完成")
        weird = self.store.add_card("随便", "", "", "not-a-kind")
        self.assertEqual(weird["kind"], "note")
        self.assertEqual(len(self.store.cards()), 2)
        self.assertTrue(self.store.delete_card(card["id"]))
        self.assertFalse(self.store.delete_card(card["id"]))
        self.assertEqual([item["title"] for item in self.store.cards()], ["随便"])

    def test_cards_require_a_title_and_stay_bounded(self):
        with self.assertRaises(ValueError):
            self.store.add_card("  ")
        for index in range(205):
            self.store.add_card(f"card {index}")
        self.assertEqual(len(self.store.cards(limit=500)), 200)


if __name__ == "__main__":
    unittest.main()
