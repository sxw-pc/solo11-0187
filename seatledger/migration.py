"""Provisional imported row count."""
from .import_rows import rows


def count(events):
    return len(rows(events))
