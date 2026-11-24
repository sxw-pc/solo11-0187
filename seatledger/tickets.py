"""Ingest offline support ticket lifecycle events."""

def cases(events):
    result = {}
    for event in events:
        key = event['ticket_id']
        result.setdefault(key, []).append(event)
    return result
