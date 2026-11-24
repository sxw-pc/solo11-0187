"""Append-only capture ledger for offline migration evidence and mappings.

Delivered mapping lists are immutable: each publish creates a new readable
version.  A late source correction never overwrites the old list; it produces
a revision list located by dependency scope (the changed rows plus every
relation that references them).  Everything lands in one SQLite transaction,
so a batch either commits with its projects and approval edges together or
rolls back leaving no partial version.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .import_rows import key_text
from .migration import plan, revision_list
from .store import canonical, digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS migration_evidence(
    event_id TEXT PRIMARY KEY, at TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_runs(
    version INTEGER PRIMARY KEY, at TEXT NOT NULL, digest TEXT NOT NULL,
    business_row_count INTEGER NOT NULL, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mapping_ledger(
    version INTEGER NOT NULL, rid TEXT NOT NULL, row TEXT NOT NULL,
    PRIMARY KEY(version, rid));
CREATE TABLE IF NOT EXISTS mapping_revisions(
    from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
    changed_rids TEXT NOT NULL, scope TEXT NOT NULL, items TEXT NOT NULL,
    PRIMARY KEY(from_version, to_version));
'''


def connect(path):
    if str(path) != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def evidence_id(event):
    if event.get('event_id'):
        return str(event['event_id'])
    if 'external_id' in event:
        return 'row:' + key_text(event)
    # a claim and its later mapping/separation correction share claim_id;
    # the kind keeps each signed document a distinct piece of evidence
    return f"gov:{event.get('kind', 'doc')}:{event.get('claim_id', digest(event))}"


def ingest_evidence(c, events):
    """Append raw export rows; identical replays are no-ops, collisions fail."""
    inserted = replayed = 0
    with c:
        for event in events:
            event_id, sha = evidence_id(event), digest(event)
            old = c.execute('SELECT digest FROM migration_evidence WHERE event_id=?',
                            (event_id,)).fetchone()
            if old:
                if old['digest'] != sha:
                    raise ValueError(f'migration evidence content collision: {event_id}')
                replayed += 1
                continue
            c.execute('INSERT INTO migration_evidence VALUES (?,?,?,?)',
                      (event_id, str(event.get('at', '')), canonical(event), sha))
            inserted += 1
    return {'inserted': inserted, 'replayed': replayed}


def stored_events(c):
    return [json.loads(r['body'])
            for r in c.execute('SELECT body FROM migration_evidence ORDER BY at,event_id')]


def latest_version(c):
    row = c.execute('SELECT MAX(version) AS v FROM migration_runs').fetchone()
    return row['v']


def publish(c, events):
    """Validate evidence and atomically commit one full migration version.

    Planning runs before any evidence write, so an invalid batch rolls back
    without leaving partial evidence or a half-published version.
    """
    manifest = plan(stored_events(c) + list(events))
    sha = manifest['digest']
    prior = c.execute('SELECT version, digest FROM migration_runs ORDER BY version DESC LIMIT 1').fetchone()
    if prior and prior['digest'] == sha:
        ingest_evidence(c, events)  # pure replay: same rows, same version
        return {'version': prior['version'], 'replayed': True, 'manifest': readback(c, prior['version'])}
    revisions = []
    with c:
        ingest_evidence(c, events)
        version = (prior['version'] if prior else 0) + 1
        manifest['version'] = version
        body = canonical(manifest)
        c.execute('INSERT INTO migration_runs VALUES (?,?,?,?,?)',
                  (version, datetime.now(timezone.utc).isoformat(), sha,
                   manifest['business_row_count'], body))
        c.executemany('INSERT INTO mapping_ledger VALUES (?,?,?)',
                      [(version, row['rid'], canonical(row)) for row in manifest['mappings']])
        if prior:
            old = readback(c, prior['version'])
            changed = {key_text(e) for e in events if 'external_id' in e}
            revisions = revision_list(old, manifest, changed)
            c.execute('INSERT INTO mapping_revisions VALUES (?,?,?,?,?)',
                      (prior['version'], version,
                       canonical(revisions['changed_rids']),
                       canonical(revisions['scope']),
                       canonical(revisions['items'])))
    return {'version': version, 'replayed': False,
            'manifest': readback(c, version), 'revisions': revisions}


def readback(c, version=None):
    """Re-read a delivered version; published mappings are never overwritten."""
    if version is None:
        version = latest_version(c)
    run = c.execute('SELECT manifest FROM migration_runs WHERE version=?', (version,)).fetchone()
    if run is None:
        raise ValueError(f'no migration version {version}')
    return json.loads(run['manifest'])


def mapping_list(c, version=None):
    if version is None:
        version = latest_version(c)
    rows = c.execute('SELECT row FROM mapping_ledger WHERE version=? ORDER BY rid', (version,))
    result = [json.loads(r['row']) for r in rows]
    if not result:
        raise ValueError(f'no mapping list for version {version}')
    return result


def revision_report(c, from_version, to_version):
    row = c.execute('SELECT * FROM mapping_revisions WHERE from_version=? AND to_version=?',
                    (from_version, to_version)).fetchone()
    if row is None:
        raise ValueError(f'no revision list {from_version}->{to_version}')
    return {'from_version': from_version, 'to_version': to_version,
            'changed_rids': json.loads(row['changed_rids']),
            'scope': json.loads(row['scope']),
            'items': json.loads(row['items'])}
