"""Per-payment fund capture against bank-terminal settlement evidence.

Layering rule: platform authorization and the platform's own
"clearing success" are not cash. Only a line inside a bank batch whose
content root verifies settles a payment. Each bank line keeps a
cross-source chain back to the platform ``entry_id``, the platform
clearing batch and the bank batch root, so a reversal sharing the
payment's reference number can still be disambiguated:

* a bank debit returning money to the customer means a customer-level
  chargeback/refund — settled gross falls and the old receipt is reversed;
* a platform reversal with no matching bank debit is only the platform
  revoking an internal duplicate clearing attempt — captured money stays;
* a platform refund claim with no bank evidence yet stays ``pending``;
* without terminal bank evidence the whole payment stays ``pending``.
  Nothing is ever inferred from a platform "success" message.

Conservation is enforced, not assumed: every bank line obeys
``gross == net + fee``; split batches re-sum to the authorization; fees
are booked separately and never treated as the customer underpaying.
"""
import hashlib
from collections import OrderedDict
from .store import canonical
from . import payments

CREDIT_KINDS = ("credit", "chargeback", "refund")
DEBIT_KINDS = ("debit",)
CUSTOMER_RETURN = "customer_return"
INTERNAL_REVOCATION = "internal_platform_revocation"
UNRESOLVED_REFUND = "unresolved_customer_refund"


def batch_root(batch):
    """Tamper-evident content root over a bank batch and its line items."""
    payload = {
        "batch_id": batch["batch_id"],
        "bank": batch.get("bank"),
        "lines": [dict(line) for line in batch["lines"]],
    }
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def verify_statements(statements):
    """Check content roots, replay and gross/net/fee balance; index lines."""
    index = OrderedDict()
    seen_batches = set()
    seen_lines = set()
    for batch in statements:
        bid = batch["batch_id"]
        if bid in seen_batches:
            raise ValueError(f"bank batch {bid} replayed under a second statement")
        seen_batches.add(bid)
        if batch.get("root") != batch_root(batch):
            raise ValueError(f"bank batch {bid} content root mismatch")
        for line in batch["lines"]:
            if line.get("type") not in CREDIT_KINDS + DEBIT_KINDS:
                raise ValueError(f"bank line {line.get('line_id')} has unknown type")
            if line["line_id"] in seen_lines:
                raise ValueError(f"bank line {line['line_id']} reused across batches")
            seen_lines.add(line["line_id"])
            gross, net, fee = line["gross"], line["net"], line["fee"]
            if gross < 0 or net < 0 or fee < 0 or gross != net + fee:
                raise ValueError(f"bank line {line['line_id']} breaks gross == net + fee")
            link = dict(line)
            link["batch_id"] = bid
            link["batch_root"] = batch["root"]
            index.setdefault(line["entry_id"], []).append(link)
    return index


def _authorized(view):
    amounts = {a["amount"] for a in view["auth"]}
    if len(amounts) > 1:
        raise ValueError(f"payment {view['entry_id']} has conflicting authorization amounts")
    return next(iter(amounts), 0)


def _classify_claims(view, debits, debit_gross):
    """Attach a bank-evidenced classification to each platform return."""
    debit_refs = {d.get("reference") for d in debits}
    unresolved = 0
    for claim in view["reversals"] + view["refunds"]:
        referenced = claim.get("reference") in debit_refs
        covered = debit_gross > 0 and claim["amount"] <= debit_gross
        if referenced or covered:
            claim["_classification"] = CUSTOMER_RETURN
        elif claim.get("kind") == "platform_refund":
            # Platform says the customer was paid back, but the bank is silent.
            claim["_classification"] = UNRESOLVED_REFUND
            unresolved += claim["amount"]
        else:
            # No money left the bank: the platform only revoked its own retry.
            claim["_classification"] = INTERNAL_REVOCATION
    return unresolved


def capture(events, statements, transfers=None):
    """Reconcile every platform payment to its bank-terminal fate.

    Returns an OrderedDict keyed by ``entry_id`` with the money buckets
    ``settled_gross`` / ``returned_gross`` / ``pending_gross``, separate
    fee and net totals, status and the full cross-source link chain.
    """
    transfers = transfers or []
    lines = verify_statements(statements)
    out = OrderedDict()

    for pid, view in payments.streams(events).items():
        auth_amount = _authorized(view)
        # Retried platform batch numbers stay inside one payment: take the
        # maximum, never a sum, so one payment cannot settle twice.
        platform_cleared = max((c["amount"] for c in view["clearing"]), default=0)
        legacy_gross = sum(x["amount"] for x in view["legacy"])
        base = auth_amount or platform_cleared or legacy_gross

        payment_lines = lines.get(pid, [])
        credits = [l for l in payment_lines if l["type"] in CREDIT_KINDS]
        debits = [l for l in payment_lines if l["type"] in DEBIT_KINDS]
        credit_gross = sum(l["gross"] for l in credits)
        debit_gross = sum(l["gross"] for l in debits)
        fees = sum(l["fee"] for l in payment_lines)
        net_received = max(sum(l["net"] for l in credits) - sum(l["net"] for l in debits), 0)

        # Bank credit may arrive in split batches or only partially; what may
        # never happen is more money settling than the customer authorized.
        if auth_amount and credit_gross > auth_amount:
            raise ValueError(
                f"payment {pid}: bank credits {credit_gross} exceed authorization {auth_amount}"
            )

        unresolved_refund = _classify_claims(view, debits, debit_gross)
        # A terminal fate exists once the bank has moved money in either
        # direction (or for a legacy bare seed entry). Platform-only noise
        # never reaches this point.
        has_terminal = bool(payment_lines) or bool(legacy_gross)

        settled = max(credit_gross + legacy_gross - debit_gross - unresolved_refund, 0)
        returned = debit_gross
        if has_terminal:
            pending = max(
                base - credit_gross - legacy_gross - debit_gross + unresolved_refund, 0
            )
        else:
            pending = base

        # Routing: a payment is bound to one invoice. Moving it to another
        # invoice needs a customer authorization matching the moved amount.
        target_invoice = view["invoice_id"]
        routes = []
        targets = set()
        for link in payment_lines:
            routed = link.get("invoice_id")
            if routed and routed != view["invoice_id"]:
                grant = next(
                    (t for t in transfers
                     if t.get("entry_id") == pid
                     and t.get("from_invoice") == view["invoice_id"]
                     and t.get("to_invoice") == routed
                     and t.get("amount") == link["gross"]
                     and t.get("status") == "authorized"),
                    None,
                )
                if grant is None:
                    raise ValueError(
                        f"payment {pid} routed to invoice {routed} without customer authorization"
                    )
                targets.add(routed)
                routes.append({
                    "line_id": link["line_id"], "from_invoice": view["invoice_id"],
                    "to_invoice": routed, "amount": link["gross"],
                    "authorization_id": grant.get("id"),
                })
        if len(targets) > 1:
            raise ValueError(f"payment {pid} cannot be split across invoices without separate authorizations")
        if targets:
            target_invoice = next(iter(targets))

        if not has_terminal:
            status = "pending"
        elif settled <= 0 and debit_gross > 0:
            status = "returned"
        elif pending > 0:
            status = "partial"
        else:
            status = "settled"

        result = {
            "entry_id": pid,
            "invoice_id": target_invoice,
            "authorized_gross": auth_amount,
            "platform_cleared_gross": platform_cleared,
            "settled_gross": settled,
            "returned_gross": returned,
            "pending_gross": pending,
            "fees": fees,
            "net_received": net_received,
            "status": status,
            "routes": routes,
            "bank_lines": [
                {
                    "line_id": l["line_id"], "batch_id": l["batch_id"],
                    "batch_root": l["batch_root"], "type": l["type"],
                    "gross": l["gross"], "net": l["net"], "fee": l["fee"],
                    "platform_batch_id": l.get("platform_batch_id"),
                    "reference": l.get("reference"),
                }
                for l in payment_lines
            ],
            "raw_platform_flows": list(view["raw"]),
        }
        # Gross conservation across the three customer-facing buckets.
        if has_terminal:
            assert settled + returned + pending == base, (pid, settled, returned, pending, base)
        else:
            assert settled == returned == 0 and pending == base
        out[pid] = result
    return out
