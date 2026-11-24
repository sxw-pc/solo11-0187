"""Match delivery acknowledgements with notices."""
from .notices import prepare


def settled(findings, receipts):
    notices = prepare(findings)
    delivered = {receipt['notice_id'] for receipt in receipts}
    return {notice_id: notice_id in delivered for notice_id in notices}
