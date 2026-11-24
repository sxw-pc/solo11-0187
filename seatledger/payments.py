"""Group offline payment-processor flows by invoice and by payment.

Platform ``platform_auth`` and ``platform_clearing`` events only prove intent:
they are never cash. Replayed events are idempotent (same id, same content),
an id reused with different content is a collision, and replacing a platform
batch number can never create a second payment — money is keyed by the
payment's ``entry_id`` and reconciled against bank-terminal batches elsewhere.
Bare seeded entries without a ``kind`` (the legacy ``payment-ok`` shape) are
terminal entries and keep their original provisional semantics.
"""
from collections import OrderedDict
from .store import canonical

PLATFORM_KINDS = (
    "platform_auth",
    "platform_clearing",
    "platform_reversal",
    "platform_refund",
)
STAGE_BY_KIND = {
    "platform_auth": "auth",
    "platform_clearing": "clearing",
    "platform_reversal": "reversals",
    "platform_refund": "refunds",
}


def dedupe(events):
    """Drop identical replays; reject same-id events with changed content."""
    seen = {}
    out = []
    for event in events:
        eid = event.get("id")
        if eid is None:
            out.append(event)
            continue
        body = canonical(event)
        if eid in seen:
            if seen[eid] != body:
                raise ValueError(f"payment event {eid} replayed with different content")
            continue
        seen[eid] = body
        out.append(event)
    return out


def entries(events):
    """Invoice_id -> platform flows touching that invoice (replay-safe)."""
    result = OrderedDict()
    for event in dedupe(events):
        result.setdefault(event["invoice_id"], []).append(event)
    return result


def streams(events):
    """Entry_id -> one payment view preserving every raw platform flow.

    Clearing retries with fresh platform batch numbers stay inside the same
    payment stream, so they can never be counted as a second payment.
    """
    result = OrderedDict()
    for event in dedupe(events):
        pid = event["entry_id"]
        view = result.setdefault(pid, {
            "entry_id": pid,
            "invoice_id": event["invoice_id"],
            "auth": [], "clearing": [], "reversals": [], "refunds": [],
            "legacy": [], "raw": [],
        })
        view["raw"].append(event)
        kind = event.get("kind")
        if kind is None:
            view["legacy"].append(event)
        elif kind in STAGE_BY_KIND:
            view[STAGE_BY_KIND[kind]].append(event)
        else:
            raise ValueError(f"payment {pid} carries unknown event kind {kind!r}")
    return result
