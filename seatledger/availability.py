"""Provisional customer journey availability verdict."""
from .journeys import runs


def available(events):
    return {run:any(row['ok'] for row in rows) for run,rows in runs(events).items()}
