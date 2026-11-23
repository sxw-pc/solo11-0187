"""Group application crashes for local triage."""
from collections import Counter
from .crashes import collect


def summary(reports):
    return dict(Counter(report['stack_hash'] for report in collect(reports)))
