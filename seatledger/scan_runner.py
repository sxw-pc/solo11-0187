"""Resumable offline RPA inventory scans with verifiable publish boundaries.

A multi-page scan is a *job*, not a list of pages.  The job journal persisted
by this module is the local evidence used after a power loss:

* the source snapshot identity and its offline continuation token (cursor),
* every accepted page original together with a content digest,
* the next expected cursor,
* every published daily-report watermark (append-only),
* any half-bundle quarantined when a source snapshot was abandoned,
* an append-only log, including the power-loss recovery probe.

Page retries are idempotent: a page is keyed by ``(snapshot, cursor)`` and an
identical retry is recorded as a replay, never as a second device observation.
A page whose snapshot differs from the active one is rejected, so an old
cursor can never be attached to a new source version.  Publishing is refused
while the job is incomplete, which keeps the last published daily report
readable.  ``recover`` chooses between the two acceptable outcomes from local
evidence only:

* a still-valid old continuation token -> finish the old snapshot at its old
  watermark (cheap, lower coverage);
* an expired token and a fully rescanable new snapshot -> quarantine the old
  half-bundle, rescan the new snapshot, and publish at the new watermark
  (higher recovery cost, higher coverage).

A successful request by itself is never treated as permission to splice pages
from different snapshots.
"""

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .installations import latest, snapshot_of
from .rollout import coverage

JOURNAL_VERSION = 1


class ScanError(ValueError):
    """Base class for invalid scan-job operations."""


class PageCursorError(ScanError):
    """A page arrived out of order."""


class SnapshotChanged(ScanError):
    """A page or cursor belongs to a different source snapshot."""


class PageConflict(ScanError):
    """One page position was retried with different content."""


class IncompleteScan(ScanError):
    """A partial inventory may not replace the published daily report."""


class WatermarkConflict(ScanError):
    """A publication would regress or reuse a watermark for other content."""


class CursorExpired(ScanError):
    """The old cursor expired and no complete new snapshot is available."""


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(obj):
    return sha256(_canonical(obj).encode('utf-8')).hexdigest()


def _snapshot_id(page):
    value = snapshot_of(page)
    if value is None:
        raise ScanError('page has no source snapshot identity')
    return value


def _atomic_write(path, state):
    """Persist the journal with a single atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path):
    """Return a defensive copy of the journal."""
    return deepcopy(json.loads(Path(path).read_text(encoding='utf-8')))


def _log(state, event, at=None, **detail):
    state['log'].append({'seq': len(state['log']) + 1, 'at': at or _now(), 'event': event, **detail})


def _write(path, state):
    _atomic_write(path, state)


def _snapshot_meta(snapshot, watermark, first_cursor, token=None, expires_at=None, extra=None):
    meta = {
        'epoch': str(snapshot),
        'watermark': int(watermark),
        'first_cursor': first_cursor,
        'continuation_token': token,
        'token_expires_at': expires_at,
    }
    if extra:
        meta.update(extra)
    return meta


def begin_job(path, *, job_id, required_build, snapshot, token=None, expires_at=None,
              first_cursor='p1', watermark=1, baseline=None, at=None):
    """Create a durable multi-page scan job.

    ``baseline`` is the last complete published daily report, for example
    ``{'snapshot': 'E1', 'watermark': 1, 'scans': [...]}``.  Keeping it in the
    journal is what makes the old report readable while a refresh is partial.
    """
    path = Path(path)
    if path.exists():
        raise ScanError(f'journal already exists: {path}')
    snapshot = str(snapshot)
    state = {
        'journal_version': JOURNAL_VERSION,
        'job_id': str(job_id),
        'required_build': required_build,
        'active_snapshot': snapshot,
        'cursor': first_cursor,
        'status': 'scanning',
        'snapshots': {
            snapshot: _snapshot_meta(snapshot, watermark, first_cursor, token, expires_at)
        },
        'pages': [],
        'quarantined': [],
        'stale_cursors': [],
        'publications': [],
        'published': None,
        'log': [],
    }
    _log(state, 'job_started', snapshot=snapshot, cursor=first_cursor, watermark=int(watermark))
    if baseline is not None:
        _install_baseline(state, baseline, at)
    _write(path, state)
    return read_state(path)


def _install_baseline(state, baseline, at):
    if isinstance(baseline, dict):
        snapshot = baseline.get('snapshot') or baseline.get('snapshot_id')
        watermark = baseline.get('watermark')
        scans = baseline.get('scans')
    else:
        snapshot, watermark, scans = None, None, baseline
    if not isinstance(scans, list) or not scans:
        raise ScanError('baseline must contain a non-empty scan list')
    watermark = int(watermark if watermark is not None
                    else state['snapshots'][state['active_snapshot']]['watermark'])
    active = state['active_snapshot']
    report_snapshot = snapshot or active
    if report_snapshot == active and watermark != state['snapshots'][active]['watermark']:
        raise WatermarkConflict('baseline watermark does not match the active snapshot')
    devices = latest(scans, snapshot=snapshot)
    publication = {
        'job_id': state['job_id'],
        'snapshot': report_snapshot,
        'watermark': watermark,
        'required_build': state['required_build'],
        'scans': _stamp([deepcopy(devices[key]) for key in sorted(devices)], report_snapshot),
        'coverage': _coverage_from_devices(devices, state['required_build']),
        'published_at': at or _now(),
        'source': 'baseline',
    }
    state['publications'].append(publication)
    state['published'] = publication
    _log(state, 'baseline_loaded', snapshot=publication['snapshot'],
         watermark=watermark, devices=len(devices))


def _coverage_from_devices(devices, required_build):
    return {
        'covered': sum(row['build'] == required_build for row in devices.values()),
        'total': len(devices),
    }


def checkpoint(path):
    """Return the offline resume point and its local evidence."""
    state = read_state(path)
    active = state['active_snapshot']
    meta = state['snapshots'][active]
    return {
        'job_id': state['job_id'],
        'status': state['status'],
        'snapshot': active,
        'cursor': state['cursor'],
        'continuation_token': meta.get('continuation_token'),
        'token_expires_at': meta.get('token_expires_at'),
        'saved_pages': len(state['pages']),
        'published_watermark': (state['published'] or {}).get('watermark'),
    }


def accept_page(path, page, at=None):
    """Persist one page original, idempotently.

    The page must carry a snapshot identity (``snapshot_id``, ``snapshot`` or
    the legacy ``epoch``) and a cursor.  An identical retry at an already
    accepted position is a replay, not a new device registration.
    """
    state = read_state(path)
    snapshot = _snapshot_id(page)
    cursor = page.get('cursor')
    if not cursor:
        raise ScanError('page has no cursor')
    active = state['active_snapshot']
    if snapshot != active:
        raise SnapshotChanged(
            f'page belongs to snapshot {snapshot!r}, job is on {active!r}; '
            'an old cursor must never be attached to a new snapshot'
        )
    if f'{snapshot}:{cursor}' in state['stale_cursors']:
        raise SnapshotChanged(
            f'cursor {cursor!r} of snapshot {snapshot!r} was retired during snapshot rotation'
        )
    if state['status'] == 'published':
        raise ScanError('job already published; a new page cannot extend it')
    scans = page.get('scans')
    if not isinstance(scans, list):
        raise ScanError('page has no scan list')
    # Fail early on contradictory device observations inside one page.
    latest(scans, snapshot=snapshot)

    page_digest = _digest({
        'snapshot': snapshot,
        'cursor': cursor,
        'next_cursor': page.get('next_cursor'),
        'scans': scans,
    })
    existing = next(
        (row for row in state['pages']
         if row['snapshot'] == snapshot and row['cursor'] == cursor),
        None,
    )
    if existing is not None:
        if existing['digest'] != page_digest:
            raise PageConflict(
                f'page {snapshot}:{cursor} was retried with different content'
            )
        _log(state, 'page_replayed', snapshot=snapshot, cursor=cursor,
             devices=len(_devices(scans)))
        _write(path, state)
        return {
            'accepted': True,
            'replayed': True,
            'snapshot': snapshot,
            'cursor': cursor,
            'next_cursor': state['cursor'],
            'saved_pages': len(state['pages']),
            'status': state['status'],
        }

    if state['status'] == 'complete':
        raise ScanError('scan already complete; a new page cannot extend it')
    if cursor != state['cursor']:
        raise PageCursorError(
            f'expected cursor {state["cursor"]!r} on snapshot {active!r}, got {cursor!r}'
        )

    next_cursor = page.get('next_cursor')
    state['pages'].append({
        'snapshot': snapshot,
        'cursor': cursor,
        'next_cursor': next_cursor,
        'scans': _stamp(scans, snapshot),
        'raw': deepcopy(page),
        'digest': page_digest,
        'accepted_at': at or _now(),
    })
    state['cursor'] = next_cursor
    _log(state, 'page_saved', snapshot=snapshot, cursor=cursor,
         next_cursor=next_cursor, scans=len(scans), digest=page_digest)
    if next_cursor is None:
        state['status'] = 'complete'
        _log(state, 'scan_completed', snapshot=snapshot, pages=len(state['pages']))
    _write(path, state)
    return {
        'accepted': True,
        'replayed': False,
        'snapshot': snapshot,
        'cursor': cursor,
        'next_cursor': next_cursor,
        'saved_pages': len(state['pages']),
        'status': state['status'],
    }


def _devices(scans):
    return {row.get('device_id') or row.get('device') or row.get('serial') for row in scans}


def _stamp(scans, snapshot):
    """Tag scan originals with the snapshot they came from.

    A published inventory must be self-describing: a reader must be able to
    verify that every row belongs to the report's watermark snapshot.
    """
    stamped = []
    for scan in scans:
        row = deepcopy(scan)
        if snapshot_of(row) is None:
            row['snapshot_id'] = snapshot
        stamped.append(row)
    return stamped


def _token_verdict(probe, state, at=None):
    """Decide token validity from local evidence, never from request success.

    A request that succeeded is not enough: the token must be the one the
    journal recorded, be explicitly accepted, and still be inside a locally
    provable expiry window.  Missing evidence is treated as unusable.
    """
    if not isinstance(probe, dict):
        return False, 'probe_missing'
    meta = state['snapshots'][state['active_snapshot']]
    stored = meta.get('continuation_token')
    if stored is None:
        return False, 'token_unproven'
    if probe.get('token') != stored:
        return False, 'token_mismatch'
    if probe.get('accepted') is not True:
        return False, probe.get('reason') or 'token_not_accepted'
    snapshot = probe.get('snapshot')
    if snapshot is not None and str(snapshot) != state['active_snapshot']:
        return False, 'probe_snapshot_mismatch'
    # The journal's expiry is authoritative.  A probe may only report an
    # earlier expiry (a server tightening the window), never extend it.
    stored_expiry = meta.get('token_expires_at')
    probe_expiry = probe.get('expires_at')
    if stored_expiry and probe_expiry and str(probe_expiry) > str(stored_expiry):
        return False, 'token_expiry_widened'
    expires = stored_expiry or probe_expiry
    if not expires:
        return False, 'token_expiry_unproven'
    checked = probe.get('checked_at') or at
    if not checked:
        return False, 'token_expiry_unproven'
    if str(checked) > str(expires):
        return False, 'token_expired'
    return True, 'token_valid'


def _prevalidate_pages(pages, snapshot):
    for page in pages:
        if _snapshot_id(page) != snapshot:
            raise SnapshotChanged(
                f'recovery page belongs to {_snapshot_id(page)!r}, expected {snapshot!r}'
            )


def _validate_complete_chain(pages, snapshot, first_cursor):
    """Refuse a "full rescan" that is itself only a prefix of the snapshot."""
    if not pages:
        raise CursorExpired('new snapshot has no pages')
    expected = first_cursor
    for page in pages:
        _snapshot_id(page)
        if page.get('cursor') != expected:
            raise PageCursorError(
                f'rescan chain broken at {expected!r}, got {page.get("cursor")!r}'
            )
        expected = page.get('next_cursor')
    if expected is not None:
        raise IncompleteScan(
            f'new snapshot rescan is incomplete; pending cursor {expected!r} on {snapshot!r}'
        )


def recover(path, probe, *, resume_pages=None, new_snapshot=None, full_pages=None, at=None):
    """Resume an interrupted job using local evidence.

    Route ``resume_snapshot`` is chosen when the old continuation token is
    still valid; the job finishes the original snapshot and publishes at the
    old watermark.  Route ``rescan_new_snapshot`` is chosen when the token is
    expired and a complete new snapshot is supplied; the old half-bundle is
    quarantined and the new snapshot is published at the new watermark.
    Anything else raises instead of splicing pages.
    """
    state = read_state(path)
    if state['status'] == 'published':
        return {
            'route': 'already_published',
            'reason': 'job_already_published',
            'snapshot': state['active_snapshot'],
            'publication': deepcopy(state['published']),
        }

    _log(state, 'power_loss_detected', snapshot=state['active_snapshot'],
         pending_cursor=state['cursor'], saved_pages=len(state['pages']))
    valid, reason = _token_verdict(probe, state, at=at)
    _log(state, 'recovery_probe', token=(probe or {}).get('token'),
         snapshot=(probe or {}).get('snapshot'), valid=valid, reason=reason)
    state['status'] = 'interrupted'
    _write(path, state)

    if valid:
        active = state['active_snapshot']
        pages = resume_pages if resume_pages is not None else (probe or {}).get('pages', [])
        _prevalidate_pages(pages, active)
        for page in pages:
            accept_page(path, page, at=at)
        state = read_state(path)
        if state['cursor'] is None and state['status'] != 'published':
            state['status'] = 'complete'
            _write(path, state)
        publication = None
        watermark = state['snapshots'][active]['watermark']
        if state['status'] == 'complete':
            publication = publish(path, at=at)
            watermark = publication['watermark']
        return {
            'route': 'resume_snapshot',
            'reason': reason,
            'status': read_state(path)['status'],
            'snapshot': active,
            'watermark': watermark,
            'quarantined_pages': 0,
            'coverage': deepcopy((publication or read_state(path)['published'] or {}).get('coverage')),
            'publication': publication,
        }

    if new_snapshot is None:
        raise CursorExpired(
            f'old cursor unusable ({reason}) and no new snapshot supplied'
        )
    meta = new_snapshot if isinstance(new_snapshot, dict) else {'id': new_snapshot}
    new_id = str(meta.get('id') or meta.get('snapshot') or meta.get('epoch'))
    if new_id == state['active_snapshot']:
        raise SnapshotChanged('a new snapshot identity is required to abandon the old cursor')
    first_cursor = meta.get('first_cursor') or (full_pages[0].get('cursor') if full_pages else 'p1')
    _prevalidate_pages(full_pages, new_id)
    _validate_complete_chain(full_pages, new_id, first_cursor)

    old_snapshot = state['active_snapshot']
    old_pages = len(state['pages'])
    if state['pages']:
        state['quarantined'].append({
            'snapshot': old_snapshot,
            'reason': reason,
            'pending_cursor': state['cursor'],
            'pages': deepcopy(state['pages']),
            'quarantined_at': at or _now(),
        })
        for row in state['pages']:
            stale = f"{row['snapshot']}:{row['cursor']}"
            if stale not in state['stale_cursors']:
                state['stale_cursors'].append(stale)
    watermark = meta.get('watermark')
    if watermark is None:
        watermark = max((p['watermark'] for p in state['publications']), default=0) + 1
    state['snapshots'][new_id] = _snapshot_meta(
        new_id, watermark, first_cursor,
        meta.get('continuation_token'), meta.get('token_expires_at'),
        {'paging_changed': bool(meta.get('paging_changed', False))},
    )
    state['pages'] = []
    state['active_snapshot'] = new_id
    state['cursor'] = state['snapshots'][new_id]['first_cursor']
    state['status'] = 'scanning'
    _log(state, 'snapshot_rotated', old_snapshot=old_snapshot, snapshot=new_id,
         reason=reason, quarantined_pages=old_pages,
         paging_changed=state['snapshots'][new_id]['paging_changed'])
    _write(path, state)

    for page in full_pages:
        accept_page(path, page, at=at)
    state = read_state(path)
    publication = None
    if state['status'] == 'complete':
        publication = publish(path, at=at)
    return {
        'route': 'rescan_new_snapshot',
        'reason': reason,
        'snapshot': new_id,
        'watermark': publication['watermark'] if publication else watermark,
        'quarantined_pages': old_pages,
        'paging_changed': state['snapshots'][new_id].get('paging_changed', False),
        'coverage': deepcopy((publication or state['published'] or {}).get('coverage')),
        'publication': publication,
    }


def publish(path, at=None):
    """Publish the active snapshot's complete inventory at its watermark.

    Refusing an incomplete job is deliberate: a partial page set must never
    replace the previously published daily report.
    """
    state = read_state(path)
    if state['status'] != 'complete':
        _log(state, 'publish_refused', reason='incomplete',
             snapshot=state['active_snapshot'], pending_cursor=state['cursor'],
             saved_pages=len(state['pages']))
        _write(path, state)
        raise IncompleteScan(
            'cannot publish an incomplete inventory; '
            f'pending cursor {state["cursor"]!r} on {state["active_snapshot"]!r}'
        )

    active = state['active_snapshot']
    scans = [deepcopy(scan) for page in state['pages'] for scan in page['scans']]
    devices = latest(scans, snapshot=active)
    inventory = _stamp([deepcopy(devices[key]) for key in sorted(devices)], active)
    watermark = int(state['snapshots'][active]['watermark'])
    coverage_value = _coverage_from_devices(devices, state['required_build'])

    if state['publications']:
        newest = state['publications'][-1]
        if watermark < int(newest['watermark']):
            raise WatermarkConflict(
                f'watermark {watermark} would regress below published {newest["watermark"]}'
            )
        if watermark == int(newest['watermark']):
            if newest['snapshot'] != active or _canonical(newest['scans']) != _canonical(inventory):
                raise WatermarkConflict(
                    f'watermark {watermark} already names a different inventory'
                )
            _log(state, 'published', snapshot=active, watermark=watermark,
                 covered=coverage_value['covered'], total=coverage_value['total'],
                 devices=len(inventory), replayed=True)
            state['status'] = 'published'
            _write(path, state)
            return {**deepcopy(newest), 'replayed': True}

    publication = {
        'job_id': state['job_id'],
        'snapshot': active,
        'watermark': watermark,
        'required_build': state['required_build'],
        'scans': inventory,
        'coverage': coverage_value,
        'published_at': at or _now(),
        'source': 'scan',
    }
    state['publications'].append(publication)
    state['published'] = publication
    state['status'] = 'published'
    _log(state, 'published', snapshot=active, watermark=watermark,
         covered=coverage_value['covered'], total=coverage_value['total'],
         devices=len(inventory))
    _write(path, state)
    return deepcopy(publication)


def read_publication(path, watermark=None):
    """Read the current or a historical published report."""
    state = read_state(path)
    if watermark is None:
        if state['published'] is None:
            raise KeyError('nothing published yet')
        return deepcopy(state['published'])
    for publication in state['publications']:
        if int(publication['watermark']) == int(watermark):
            return deepcopy(publication)
    raise KeyError(f'watermark {watermark} was never published')


def read_log(path):
    """Read the append-only recovery/power-loss log."""
    return deepcopy(read_state(path)['log'])


def saved_pages(path):
    """Return the accepted page originals of the active snapshot."""
    state = read_state(path)
    active = state['active_snapshot']
    return [deepcopy(row) for row in state['pages'] if row['snapshot'] == active]


def quarantined_pages(path):
    """Return half-bundles retained after an abandoned snapshot."""
    return deepcopy(read_state(path)['quarantined'])


# Convenience entry point retained for the original single-import API.
def run(pages, required_build, published=None, snapshot=None):
    """Summarize a one-snapshot page set, or fall back to a published report.

    Flattening mixed-epoch pages is exactly the defect this module guards
    against, so it raises :class:`~seatledger.installations.SnapshotMismatch`
    unless the caller supplies the last published complete inventory.  In that
    case the published inventory -- not the spliced pages -- is summarized.
    """
    return coverage(pages, required_build, published=published, snapshot=snapshot)
