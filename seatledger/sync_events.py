"""Collect offline document edit events by document."""


def edits(events):
    result={}
    for event in events:
        result.setdefault(event['document_id'], []).append(event)
    return result
