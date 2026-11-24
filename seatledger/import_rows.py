"""Collect offline source application rows by external ID."""


def rows(events):
    result={}
    for event in events:
        result[event['external_id']]=event
    return result
