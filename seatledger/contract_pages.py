"""Collect offline OCR page blocks by contract."""


def pages(events):
    result={}
    for event in events:
        result.setdefault(event['contract_id'], []).append(event)
    return result
