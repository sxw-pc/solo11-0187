"""Group offline payment processor entries by invoice."""


def entries(events):
    result={}
    for event in events:
        result.setdefault(event['invoice_id'], []).append(event)
    return result
