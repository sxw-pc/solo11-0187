"""Collect offline synthetic user-journey step events."""


def runs(events):
    result={}
    for event in events:
        result.setdefault(event['run_id'], []).append(event)
    return result
