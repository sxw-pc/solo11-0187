"""Collect offline source rows by composite identity.

Record identity is ``(source_system, tenant, source_no)``.  Both legacy
systems number rows from 1, so a bare ``external_id`` key lets a later row
overwrite an unrelated tenant (and shrinks the migratable count).  The last
row for a composite key still wins, preserving the original replay rule.
"""

LEGACY_SOURCE = 'legacy'


def source_no(event):
    return str(event.get('source_no', event['external_id']))


def composite_key(event):
    return (str(event.get('source_system', LEGACY_SOURCE)),
            str(event['tenant']), source_no(event))


def key_text(event):
    return '/'.join(composite_key(event))


def ref_rid(ref):
    """Normalize a ``system/tenant/number`` reference (or row dict) to a rid."""
    if isinstance(ref, dict):
        return key_text(ref)
    parts = str(ref).split('/')
    if len(parts) != 3 or not all(parts):
        raise ValueError(f'bad reference {ref!r}, expected system/tenant/number')
    return '/'.join(parts)


def rows(events):
    """Last row per composite identity.

    Governance documents (identity claims/mappings/separations) carry no
    source row number and are not migratable business records.
    """
    result = {}
    for event in events:
        if 'external_id' not in event:
            continue
        result[composite_key(event)] = event
    return result


def global_rows(events):
    """Pre-fix behaviour kept for regression evidence: bare external id key."""
    result = {}
    for event in events:
        if 'external_id' in event:
            result[str(event['external_id'])] = event
    return result
