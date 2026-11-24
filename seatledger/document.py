"""Provisional last-write-wins document text."""
from .sync_events import edits


def text(events):
    return {doc:max(rows,key=lambda row:row['clock'])['content'] for doc,rows in edits(events).items()}
