"""票据：绑定 pane/路径/方向/大小，过期和用量都记账。"""
import time
import unittest

import transfer_tickets as tickets

SECRET = b"unit-test-transfer-secret"


class MintAndVerifyTests(unittest.TestCase):
    def test_a_ticket_round_trips_with_its_claims(self):
        grant = tickets.mint(SECRET, pane="%4", path="/srv/a.bin", direction="down", max_bytes=0)
        payload = tickets.peek(SECRET, grant["token"])
        self.assertEqual(payload["p"], "%4")
        self.assertEqual(payload["f"], "/srv/a.bin")
        self.assertEqual(payload["d"], "down")
        self.assertGreater(payload["e"], int(time.time()))

    def test_an_upload_ticket_carries_its_size_ceiling_and_name(self):
        grant = tickets.mint(SECRET, pane="%4", path="/uploads/x.bin", direction="up",
                             max_bytes=9 * 1024 * 1024, name="x.bin")
        payload = tickets.peek(SECRET, grant["token"])
        self.assertEqual(payload["m"], 9 * 1024 * 1024)
        self.assertEqual(payload["n"], "x.bin")

    def test_a_tampered_body_is_rejected(self):
        grant = tickets.mint(SECRET, pane="%4", path="/srv/a.bin", direction="down")
        body, signature = grant["token"].split(".", 1)
        forged = tickets._encode({"p": "%4", "f": "/root/.ssh/id_rsa", "d": "down",
                                  "m": 0, "e": int(time.time()) + 60, "u": 8, "i": "abc"})
        with self.assertRaises(tickets.TicketError):
            tickets.peek(SECRET, forged + "." + signature)
        with self.assertRaises(tickets.TicketError):
            tickets.peek(SECRET, body + "." + "0" * 32)

    def test_another_secret_cannot_verify(self):
        grant = tickets.mint(SECRET, pane="%4", path="/srv/a.bin", direction="down")
        with self.assertRaises(tickets.TicketError):
            tickets.peek(b"other-secret", grant["token"])

    def test_an_expired_ticket_is_rejected(self):
        grant = tickets.mint(SECRET, pane="%4", path="/srv/a.bin", direction="down", ttl=-1)
        with self.assertRaises(tickets.TicketError):
            tickets.peek(SECRET, grant["token"])

    def test_garbage_is_rejected_not_crashed(self):
        for token in ("", "no-dot", "a.b.c", "!!!.???"):
            with self.assertRaises(tickets.TicketError):
                tickets.peek(SECRET, token)

    def test_minting_requires_a_pane_path_and_known_direction(self):
        with self.assertRaises(ValueError):
            tickets.mint(SECRET, pane="", path="/x", direction="down")
        with self.assertRaises(ValueError):
            tickets.mint(SECRET, pane="%4", path="", direction="down")
        with self.assertRaises(ValueError):
            tickets.mint(SECRET, pane="%4", path="/x", direction="sideways")


class LedgerTests(unittest.TestCase):
    def test_uses_are_counted_until_they_run_out(self):
        ledger = tickets.Ledger()
        payload = {"i": "nonce-1", "u": 2, "e": int(time.time()) + 60}
        ledger.spent(payload)
        ledger.spent(payload)
        with self.assertRaises(tickets.TicketError):
            ledger.spent(payload)

    def test_an_upload_ticket_is_worth_several_chunks(self):
        ledger = tickets.Ledger()
        payload = {"i": "nonce-2", "u": 5, "e": int(time.time()) + 60}
        for _ in range(5):
            ledger.spent(payload)
        with self.assertRaises(tickets.TicketError):
            ledger.spent(payload)

    def test_expired_entries_are_pruned(self):
        ledger = tickets.Ledger(limit=4)
        for index in range(8):
            ledger.spent({"i": f"old-{index}", "u": 1, "e": int(time.time()) - 10})
        self.assertLessEqual(len(ledger._seen), 4)

    def test_a_reset_clears_everything(self):
        ledger = tickets.Ledger()
        ledger.spent({"i": "nonce-3", "u": 1, "e": int(time.time()) + 60})
        ledger.reset()
        ledger.spent({"i": "nonce-3", "u": 1, "e": int(time.time()) + 60})    # 不抛就说明清了


if __name__ == "__main__":
    unittest.main()
