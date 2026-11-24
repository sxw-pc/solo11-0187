"""Apply bank-captured funds to implementation-service invoices.

Only bank-terminal captured money may clear an invoice. Each invoice is
reported in three money states that always conserve to its face amount:

* ``received``   — customer funds finally in the bank (gross; the fee is
  disclosed separately, never netted against the customer's obligation);
* ``receivable`` — still owed;
* ``pending``    — claimed by platform flows without terminal bank proof.

Customer receipts are append-only evidence: an issued receipt is never
overwritten. A chargeback or a late bank slip produces a traceable
reversal entry plus a fresh terminal receipt/advice, and provisional
receipts stay provisional until bank evidence arrives. This module never
touches license entitlements or any prior recovery ruling.
"""
from collections import OrderedDict
from . import payment_capture
from .payments import dedupe

SETTLED = "settled"
PENDING = "pending"
RECEIVABLE = "receivable"
STATUS_ZH = {SETTLED: "已到账", PENDING: "待核", RECEIVABLE: "仍应收"}


def load_bundle(bundle):
    """Normalize an offline evidence bundle into its component streams."""
    return (
        bundle.get("events", []),
        bundle.get("statements", []),
        bundle.get("transfer_authorizations", bundle.get("transfers", [])),
        bundle.get("confirmations", []),
        bundle["invoices"],
    )


def invoice_positions(captured, invoice_amounts):
    """Allocate captured payments to invoices and derive the three states."""
    received, pending = {}, {}
    for pay in captured.values():
        inv = pay["invoice_id"]
        received[inv] = received.get(inv, 0) + pay["settled_gross"]
        pending[inv] = pending.get(inv, 0) + pay["pending_gross"]

    positions = OrderedDict()
    for invoice, amount in invoice_amounts.items():
        got = received.get(invoice, 0)
        claimed = pending.get(invoice, 0)
        applied = min(got, amount)
        receivable = amount - applied
        held = min(claimed, receivable)
        receivable -= held
        if applied >= amount:
            status = SETTLED
        elif held > 0:
            status = PENDING
        else:
            status = RECEIVABLE
        positions[invoice] = {
            "invoice_id": invoice,
            "amount": amount,
            "received": applied,
            "pending": held,
            "receivable": receivable,
            "status": status,
            "status_zh": STATUS_ZH[status],
            "gross_received": got,
            "fees": sum(
                p["fees"] for p in captured.values() if p["invoice_id"] == invoice
            ),
            "net_received": sum(
                p["net_received"] for p in captured.values() if p["invoice_id"] == invoice
            ),
            "payment_entries": [p["entry_id"] for p in captured.values()
                                if p["invoice_id"] == invoice],
        }
        # Invoice-level conservation: the three buckets re-sum to face value.
        assert applied + held + receivable == amount
    return positions


def confirmation_ledger(captured, prior_receipts):
    """Build the append-only receipt/reversal/advice trail per payment."""
    entries = []
    covered = set()

    def add(entry):
        if entry["id"] not in covered:
            covered.add(entry["id"])
            entries.append(entry)

    provisional, superseded = [], []
    for receipt in dedupe(prior_receipts):
        add(dict(receipt))
        pay = captured.get(receipt["entry_id"])
        if pay is None or not (pay["bank_lines"] or pay["status"] == "settled"):
            provisional.append(receipt["id"])
            continue
        reversal_id = f"{receipt['id']}:reversal"
        if pay["settled_gross"] > 0:
            # Bank terminal evidence confirms the money: replace the platform
            # receipt with one rooted in bank batches, leaving the old one in place.
            add({
                "id": reversal_id,
                "kind": "receipt_reversal",
                "reverses": receipt["id"],
                "reason": "bank_terminal_supersede",
                "entry_id": receipt["entry_id"],
                "invoice_id": receipt["invoice_id"],
                "amount": receipt["amount"],
                "batch_refs": [
                    {"batch_id": l["batch_id"], "batch_root": l["batch_root"],
                     "line_id": l["line_id"]}
                    for l in pay["bank_lines"] if l["type"] in payment_capture.CREDIT_KINDS
                ],
            })
            add({
                "id": f"{receipt['entry_id']}:terminal-receipt",
                "kind": "receipt",
                "origin": "bank_terminal",
                "entry_id": receipt["entry_id"],
                "invoice_id": pay["invoice_id"],
                "amount": pay["settled_gross"],
                "fees": pay["fees"],
                "net_received": pay["net_received"],
                "batch_refs": [
                    {"batch_id": l["batch_id"], "batch_root": l["batch_root"],
                     "line_id": l["line_id"]}
                    for l in pay["bank_lines"] if l["type"] in payment_capture.CREDIT_KINDS
                ],
                "supersedes": receipt["id"],
            })
            superseded.append(receipt["id"])
        else:
            # Money went back: reverse the old receipt and issue return advice.
            add({
                "id": reversal_id,
                "kind": "receipt_reversal",
                "reverses": receipt["id"],
                "reason": "customer_chargeback",
                "entry_id": receipt["entry_id"],
                "invoice_id": receipt["invoice_id"],
                "amount": receipt["amount"],
                "returned_gross": pay["returned_gross"],
                "batch_refs": [
                    {"batch_id": l["batch_id"], "batch_root": l["batch_root"],
                     "line_id": l["line_id"]}
                    for l in pay["bank_lines"] if l["type"] in payment_capture.DEBIT_KINDS
                ],
            })
            add({
                "id": f"{receipt['entry_id']}:return-advice",
                "kind": "return_advice",
                "entry_id": receipt["entry_id"],
                "invoice_id": receipt["invoice_id"],
                "amount": pay["returned_gross"],
                "batch_refs": [
                    {"batch_id": l["batch_id"], "batch_root": l["batch_root"],
                     "line_id": l["line_id"]}
                    for l in pay["bank_lines"] if l["type"] in payment_capture.DEBIT_KINDS
                ],
            })

    # Payments that reached a terminal fate without any earlier customer receipt.
    for pid, pay in captured.items():
        if any(r["entry_id"] == pid for r in dedupe(prior_receipts)):
            continue
        if pay["settled_gross"] > 0:
            add({
                "id": f"{pid}:terminal-receipt",
                "kind": "receipt",
                "origin": "bank_terminal",
                "entry_id": pid,
                "invoice_id": pay["invoice_id"],
                "amount": pay["settled_gross"],
                "fees": pay["fees"],
                "net_received": pay["net_received"],
                "batch_refs": [
                    {"batch_id": l["batch_id"], "batch_root": l["batch_root"],
                     "line_id": l["line_id"]}
                    for l in pay["bank_lines"] if l["type"] in payment_capture.CREDIT_KINDS
                ],
            })
        elif pay["returned_gross"] > 0:
            add({
                "id": f"{pid}:return-advice",
                "kind": "return_advice",
                "entry_id": pid,
                "invoice_id": pay["invoice_id"],
                "amount": pay["returned_gross"],
                "batch_refs": [
                    {"batch_id": l["batch_id"], "batch_root": l["batch_root"],
                     "line_id": l["line_id"]}
                    for l in pay["bank_lines"] if l["type"] in payment_capture.DEBIT_KINDS
                ],
            })

    current = {}
    for entry in entries:
        if entry["kind"] == "receipt":
            # A provisional platform receipt without bank-terminal proof is
            # retained as evidence but is not a current confirmed receipt.
            if entry.get("origin") == "platform" and entry["id"] in provisional:
                continue
            current[entry["entry_id"]] = entry["id"]
        elif entry["kind"] == "receipt_reversal":
            current.pop(entry["entry_id"], None)
    return {
        "entries": entries,
        "current_receipts": current,
        "provisional": provisional,
        "superseded": superseded,
    }


def apply_bundle(bundle):
    """Full per-invoice cash application with evidence and receipt trail."""
    events, statements, transfers, receipts, invoices = load_bundle(bundle)
    captured = payment_capture.capture(events, statements, transfers)
    return {
        "invoices": invoice_positions(captured, invoices),
        "payments": captured,
        "confirmations": confirmation_ledger(captured, receipts),
    }


def paid(events, invoice_amounts, statements=(), transfers=(), confirmations=()):
    """Boolean paid flags; ``False`` covers both still-receivable and pending."""
    captured = payment_capture.capture(events, statements, transfers)
    positions = invoice_positions(captured, invoice_amounts)
    return {invoice: pos["status"] == SETTLED for invoice, pos in positions.items()}
