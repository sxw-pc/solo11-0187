"""Collect offline feature experiment exposures by subject."""


def assignments(events):
    result={}
    for event in events:
        result[event['subject_id']]=event['arm']
    return result
