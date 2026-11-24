"""Provisional invoice paid-state from processor entries."""
from .payments import entries


def paid(events, invoice_amounts):
    grouped=entries(events)
    return {invoice:sum(row['amount'] for row in grouped.get(invoice,[]))>=amount for invoice,amount in invoice_amounts.items()}
