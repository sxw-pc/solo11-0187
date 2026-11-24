"""Provisional implementation milestone extractor."""
from .contract_pages import pages


def milestones(events):
    result={}
    for contract,rows in pages(events).items():
        dates=[row['date'] for row in rows if row['label']=='delivery']
        result[contract]=dates[-1] if dates else None
    return result
