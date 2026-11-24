import copy
import json
import unittest
from pathlib import Path

from seatledger.cash_application import (
    apply_bundle, invoice_positions, paid, SETTLED, PENDING, RECEIVABLE,
)
from seatledger.payment_capture import batch_root, capture, verify_statements
from seatledger.payments import entries, streams, dedupe

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class LegacySeedTest(unittest.TestCase):
    def test_bare_seed_still_reports_paid(self):
        # The original fixed enhancement copy behaviour must survive.
        data = fixture("payment-ok.json")
        self.assertEqual(paid(data["events"], data["invoices"]), {"I1": True})
        grouped = entries(data["events"])
        self.assertEqual([e["entry_id"] for e in grouped["I1"]], ["P1"])


class PlatformOnlyTest(unittest.TestCase):
    def setUp(self):
        self.data = fixture("payment-clearing.json")

    def test_auth_and_platform_success_are_not_cash(self):
        # No bank statement at all: platform "clearing success" must not clear.
        flags = paid(self.data["events"], self.data["invoices"])
        self.assertEqual(flags, {"I1": False, "I2": False})
        captured = capture(self.data["events"], [])
        self.assertEqual(captured["P1"]["status"], PENDING)
        self.assertEqual(captured["P1"]["pending_gross"], 100)
        self.assertEqual(captured["P1"]["settled_gross"], 0)
        # Filtering only negative refunds still cannot clear the invoice.
        positive_only = [e for e in self.data["events"] if e.get("amount", 0) >= 0]
        self.assertEqual(paid(positive_only, self.data["invoices"]),
                         {"I1": False, "I2": False})

    def test_auth_downgrade_returns_true_to_pending(self):
        # P1's +100 re-adjudicated from terminal entry to authorization only.
        data = fixture("payment-ok.json")
        self.assertTrue(paid(data["events"], data["invoices"])["I1"])
        auth_only = [{"id": "X1", "kind": "platform_auth", "at": "2026-11-20T09:00",
                      **{k: v for k, v in data["events"][0].items() if k != "id"}}]
        result = apply_bundle({"invoices": data["invoices"], "events": auth_only})
        self.assertEqual(result["invoices"]["I1"]["status"], PENDING)
        self.assertEqual(paid(auth_only, data["invoices"])["I1"], False)

    def test_replay_is_idempotent_but_content_collision_rejected(self):
        repeated = self.data["events"] + [copy.deepcopy(self.data["events"][0])]
        self.assertEqual(len(dedupe(repeated)), len(self.data["events"]))
        bad = copy.deepcopy(self.data["events"][0])
        bad["amount"] = 999
        with self.assertRaises(ValueError):
            dedupe(self.data["events"] + [bad])

    def test_replaced_platform_batch_number_cannot_settle_twice(self):
        # A retry under a new platform batch id stays inside the P1 stream.
        retry = {"id": "E2b", "kind": "platform_clearing", "at": "2026-11-20T09:10",
                 "entry_id": "P1", "invoice_id": "I1", "amount": 100,
                 "platform_batch_id": "C-001-RETRY", "reference": "R-P1",
                 "status": "success"}
        views = streams(self.data["events"] + [retry])
        self.assertEqual(list(views), ["P1", "P2"])
        captured = capture(self.data["events"] + [retry], [])
        self.assertEqual(captured["P1"]["platform_cleared_gross"], 100)
        self.assertEqual(captured["P1"]["pending_gross"], 100)


class BankTerminalTest(unittest.TestCase):
    def setUp(self):
        self.data = fixture("payment-clearing.json")

    def test_path_a_split_batches_fees_and_internal_reversal_clear_invoice(self):
        bundle = {
            **self.data,
            "statements": self.data["path_a_terminal"]["statements"],
        }
        result = apply_bundle(bundle)
        captured = result["payments"]

        # 97 net + 3 fee proves the full 100 gross belongs to P1.
        p1 = captured["P1"]
        self.assertEqual(p1["status"], SETTLED)
        self.assertEqual(p1["settled_gross"], 100)
        self.assertEqual(p1["net_received"], 97)
        self.assertEqual(p1["fees"], 3)
        self.assertEqual(
            {f["_classification"] for f in self.data["events"][2:3]},
            {"internal_platform_revocation"},
        )

        # P2 is split across two bank batches that re-sum to 200.
        p2 = captured["P2"]
        self.assertEqual(len(p2["bank_lines"]), 2)
        self.assertEqual(p2["settled_gross"], 200)
        self.assertEqual(p2["net_received"], 198)
        self.assertEqual(p2["fees"], 2)

        positions = result["invoices"]
        self.assertEqual(positions["I1"]["status"], SETTLED)
        self.assertEqual(positions["I1"]["received"], 100)
        self.assertEqual(positions["I1"]["receivable"], 0)
        self.assertEqual(positions["I1"]["pending"], 0)
        self.assertEqual(positions["I2"]["status"], SETTLED)
        # The fee is disclosed, never treated as the customer underpaying.
        self.assertEqual(positions["I2"]["received"], 200)
        self.assertEqual(positions["I2"]["net_received"], 198)
        for pos in positions.values():
            self.assertEqual(pos["received"] + pos["pending"] + pos["receivable"],
                             pos["amount"])
        self.assertTrue(paid(self.data["events"], self.data["invoices"],
                             self.data["path_a_terminal"]["statements"])["I1"])

    def test_path_a_every_bank_line_keeps_cross_source_chain(self):
        captured = capture(self.data["events"],
                           self.data["path_a_terminal"]["statements"])
        line = captured["P1"]["bank_lines"][0]
        self.assertEqual(line["platform_batch_id"], "C-001")
        self.assertEqual(line["reference"], "R-P1")
        self.assertTrue(line["batch_root"])
        batch = self.data["path_a_terminal"]["statements"][0]
        self.assertEqual(line["batch_root"], batch["root"])

    def test_path_b_chargeback_keeps_invoice_receivable_and_reverses_receipt(self):
        events = self.data["events"] + self.data["path_b_terminal"]["extra_events"]
        statements = self.data["path_b_terminal"]["statements"]
        result = apply_bundle({
            **self.data, "events": events, "statements": statements,
        })
        p1 = result["payments"]["P1"]
        self.assertEqual(p1["status"], "returned")
        self.assertEqual(p1["settled_gross"], 0)
        self.assertEqual(p1["returned_gross"], 100)
        self.assertEqual(p1["pending_gross"], 0)

        i1 = result["invoices"]["I1"]
        self.assertEqual(i1["status"], RECEIVABLE)
        self.assertEqual(i1["receivable"], 100)
        self.assertEqual(i1["received"], 0)
        # I2 still clears on the same initial flows; only I1's conclusion flips.
        self.assertEqual(result["invoices"]["I2"]["status"], SETTLED)

        ledger = result["confirmations"]
        ids = [e["id"] for e in ledger["entries"]]
        # The already-issued customer receipt is never overwritten...
        self.assertIn("K1", ids)
        original = next(e for e in ledger["entries"] if e["id"] == "K1")
        self.assertEqual(original["amount"], 100)
        # ...it is reversed with a traceable entry and a fresh advice follows.
        reversal = next(e for e in ledger["entries"] if e["kind"] == "receipt_reversal")
        self.assertEqual(reversal["reverses"], "K1")
        self.assertEqual(reversal["reason"], "customer_chargeback")
        self.assertTrue(reversal["batch_refs"])
        advice = next(e for e in ledger["entries"] if e["kind"] == "return_advice")
        self.assertEqual(advice["amount"], 100)
        self.assertNotIn("P1", ledger["current_receipts"])

    def test_old_receipt_is_superseded_not_overwritten_on_settlement(self):
        result = apply_bundle({
            **self.data,
            "statements": self.data["path_a_terminal"]["statements"],
        })
        ledger = result["confirmations"]
        ids = [e["id"] for e in ledger["entries"]]
        self.assertIn("K1", ids)
        self.assertIn("K1:reversal", ids)
        self.assertIn("P1:terminal-receipt", ids)
        self.assertEqual(ledger["current_receipts"]["P1"], "P1:terminal-receipt")
        terminal = next(e for e in ledger["entries"] if e["id"] == "P1:terminal-receipt")
        self.assertEqual(terminal["supersedes"], "K1")
        self.assertEqual(terminal["fees"], 3)

    def test_receipt_without_bank_proof_stays_provisional(self):
        result = apply_bundle(self.data)
        ledger = result["confirmations"]
        self.assertEqual(ledger["provisional"], ["K1"])
        self.assertNotIn("P1", ledger["current_receipts"])
        self.assertEqual(result["invoices"]["I1"]["status"], PENDING)


class BankIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.data = fixture("payment-clearing.json")
        self.statements = copy.deepcopy(self.data["path_a_terminal"]["statements"])

    def test_tampered_batch_root_is_rejected(self):
        self.statements[0]["lines"][0]["net"] = 10  # alter without re-rooting
        with self.assertRaises(ValueError):
            verify_statements(self.statements)

    def test_fee_cannot_break_conservation(self):
        self.statements[0]["lines"][0]["net"] = 96  # 96 + 3 != 100
        self.statements[0]["root"] = batch_root(self.statements[0])
        with self.assertRaises(ValueError):
            verify_statements(self.statements)

    def test_split_batch_cannot_credit_more_than_authorization(self):
        self.statements[0]["lines"][1]["gross"] = 130
        self.statements[0]["lines"][1]["net"] = 130
        self.statements[0]["root"] = batch_root(self.statements[0])
        with self.assertRaises(ValueError):
            capture(self.data["events"], self.statements)

    def test_partial_bank_capture_conserves_buckets(self):
        # Only 60 of the authorized 100 reaches a terminal bank batch.
        data = {
            "invoices": {"I": 100},
            "events": [{
                "id": "a", "kind": "platform_auth", "at": "t1", "entry_id": "P",
                "invoice_id": "I", "amount": 100, "reference": "R",
            }],
        }
        batch = {
            "batch_id": "BP", "bank": "B", "root": None,
            "lines": [{"line_id": "l2", "entry_id": "P", "type": "credit",
                       "gross": 60, "net": 60, "fee": 0,
                       "platform_batch_id": "C", "reference": "R"}],
        }
        batch["root"] = batch_root(batch)
        result = apply_bundle({**data, "statements": [batch]})
        pos = result["invoices"]["I"]
        self.assertEqual(pos["status"], PENDING)
        self.assertEqual((pos["received"], pos["pending"], pos["receivable"]),
                         (60, 40, 0))
        self.assertEqual(pos["received"] + pos["pending"] + pos["receivable"], 100)

    def test_replayed_bank_batch_or_line_is_rejected(self):
        dup = self.statements + [copy.deepcopy(self.statements[1])]
        with self.assertRaises(ValueError):
            verify_statements(dup)


class CrossInvoiceRoutingTest(unittest.TestCase):
    def _bundle(self, routed_invoice, transfers):
        events = [{
            "id": "T1", "kind": "platform_auth", "at": "2026-11-23T08:00",
            "entry_id": "P9", "invoice_id": "I3", "amount": 100, "reference": "R-P9",
        }, {
            "id": "T2", "kind": "platform_clearing", "at": "2026-11-23T08:05",
            "entry_id": "P9", "invoice_id": "I3", "amount": 100,
            "platform_batch_id": "C-009", "reference": "R-P9", "status": "success",
        }]
        batch = {
            "batch_id": "B-T1", "bank": "TerminalBank", "root": None,
            "lines": [{
                "line_id": "B-T1-1", "entry_id": "P9", "type": "credit",
                "gross": 100, "net": 97, "fee": 3,
                "platform_batch_id": "C-009", "reference": "R-P9",
                "invoice_id": routed_invoice,
            }],
        }
        batch["root"] = batch_root(batch)
        return {"invoices": {"I3": 100, "I4": 100}, "events": events,
                "statements": [batch], "transfer_authorizations": transfers}

    def test_routing_without_customer_authorization_rejected(self):
        with self.assertRaises(ValueError):
            apply_bundle(self._bundle("I4", []))

    def test_authorized_routing_moves_money_once(self):
        grant = [{"id": "A1", "status": "authorized", "entry_id": "P9",
                  "from_invoice": "I3", "to_invoice": "I4", "amount": 100}]
        result = apply_bundle(self._bundle("I4", grant))
        self.assertEqual(result["payments"]["P9"]["invoice_id"], "I4")
        self.assertEqual(result["invoices"]["I3"]["status"], RECEIVABLE)
        self.assertEqual(result["invoices"]["I3"]["receivable"], 100)
        self.assertEqual(result["invoices"]["I4"]["status"], SETTLED)
        # One payment can never settle two invoices.
        self.assertEqual(result["invoices"]["I4"]["received"], 100)
        self.assertEqual(result["invoices"]["I3"]["received"], 0)


if __name__ == "__main__":
    unittest.main()
