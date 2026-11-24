"""Measure the first response against a simple response deadline."""
from .tickets import cases


def overdue(events):
    result = {}
    for ticket, rows in cases(events).items():
        created = min(row['minute'] for row in rows if row['kind'] == 'opened')
        response = min((row['minute'] for row in rows if row['kind'] == 'reply'), default=None)
        result[ticket] = response is None or response - created > 60
    return result
