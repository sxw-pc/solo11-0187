"""Capture offline-recovered experiment events with explicit capture time.

Every raw event keeps its own event time (`at`); the ledger records when the
event entered the ledger (`captured_at`). Analysis versions pin the set of
events captured so far, so a late回传 event can only ever produce a new
analysis version and never rewrites an earlier one. Replay of an identical
event is idempotent; the same id with different content is rejected.
"""
from datetime import datetime
from .store import canonical, digest

KINDS = ('assignment', 'exposure', 'conversion', 'identity')
REQUIRED = {
    'assignment': ('id', 'kind', 'at', 'experiment', 'org_id', 'arm', 'subjects', 'domain'),
    'exposure': ('id', 'kind', 'at', 'experiment', 'subject_id', 'device_id', 'arm'),
    'conversion': ('id', 'kind', 'at', 'experiment', 'subject_id'),
    'identity': ('id', 'kind', 'at', 'experiment', 'authority', 'subjects', 'conclusion', 'verified'),
}
AUTHORITIES = ('tenant_signing', 'contract_isolation')


def _check_time(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be an ISO timestamp string')
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{field} is not an ISO timestamp: {value!r}')


def normalize(event, experiment, captured_at):
    """Validate one raw event and stamp it with its capture time."""
    if not isinstance(event, dict):
        raise ValueError('event must be a mapping')
    kind = event.get('kind')
    if kind not in KINDS:
        raise ValueError(f'unknown event kind: {kind!r}')
    missing = [field for field in REQUIRED[kind] if field not in event]
    if missing:
        raise ValueError(f'event missing fields: {missing}')
    if event['experiment'] != experiment:
        raise ValueError('event experiment mismatch')
    _check_time(event['at'], 'at')
    _check_time(captured_at, 'captured_at')
    if kind == 'assignment' and event['arm'] not in ('A', 'B'):
        raise ValueError('assignment arm must be A or B')
    if kind == 'exposure' and event['arm'] not in ('A', 'B'):
        raise ValueError('exposure arm must be A or B')
    if kind == 'identity':
        if event['authority'] not in AUTHORITIES:
            raise ValueError(f'identity authority must be one of {AUTHORITIES}')
        if event['conclusion'] not in ('same_org', 'independent_orgs'):
            raise ValueError('identity conclusion must be same_org or independent_orgs')
    body = canonical(event)
    return {'event': event, 'digest': digest(event), 'captured_at': captured_at, 'body': body}


class CaptureLedger:
    """Append-only capture log for one experiment's offline events."""

    def __init__(self, experiment):
        self.experiment = experiment
        self._by_id = {}

    def ingest(self, events, captured_at):
        if not isinstance(events, list) or not events:
            raise ValueError('nonempty event list required')
        inserted = replayed = 0
        staged = []
        for event in events:
            record = normalize(event, self.experiment, captured_at)
            old = self._by_id.get(event['id'])
            if old:
                if old['digest'] != record['digest']:
                    raise ValueError('event id content collision')
                replayed += 1
                continue
            staged.append(record)
        for record in staged:
            self._by_id[record['event']['id']] = record
            inserted += 1
        return {'inserted': inserted, 'replayed': replayed}

    def events(self, kind=None):
        rows = sorted(self._by_id.values(), key=lambda r: (r['event']['at'], r['event']['id']))
        result = []
        for record in rows:
            if kind and record['event']['kind'] != kind:
                continue
            result.append(dict(record['event'], captured_at=record['captured_at']))
        return result

    def count(self):
        return len(self._by_id)
