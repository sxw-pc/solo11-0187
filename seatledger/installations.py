"""Collect offline product installation scans.

The important rule in this module is that "latest" only means *latest inside
one source snapshot*.  Two pages taken from different source epochs are not a
single inventory, even when they happen to contain different devices.  Mixing
them hides a missing page and makes a partial scan look complete.
"""

from copy import deepcopy


class SnapshotMismatch(ValueError):
    """Scans from more than one source snapshot were passed as one inventory."""


class DeviceConflict(ValueError):
    """One stable device has two contradictory observations at one instant."""


_SNAPSHOT_KEYS = ('snapshot_id', 'snapshot', 'epoch')


def snapshot_of(scan):
    """Return the source snapshot identity attached to a scan, if any.

    The keys are listed separately so conflicting aliases can be detected
    instead of silently preferring one of them.
    """
    for key in _SNAPSHOT_KEYS:
        value = scan.get(key)
        if value is not None:
            return str(value)
    return None


def snapshot_aliases(scan):
    """Return every distinct snapshot alias value; more than one is invalid."""
    return {str(scan[key]) for key in _SNAPSHOT_KEYS if scan.get(key) is not None}


def device_identity(scan):
    """Return the stable identity of a terminal.

    ``device_id`` is preferred.  The older fixtures use ``device``; a serial
    number is accepted as a last resort so that a renamed host cannot silently
    consume a second seat.
    """
    for key in ('device_id', 'device', 'serial'):
        value = scan.get(key)
        if value not in (None, ''):
            return str(value)
    raise KeyError('scan has no stable device identity')


def _same_observation(left, right):
    return left == right


def _newer(scan, current):
    """Return whether the new observation should replace ``current``.

    Timestamps are ISO-8601 strings in the fixture set.  Replaying the same
    page produces an identical observation and is harmless.  Two different
    builds for the same device at the same instant is not a race we may guess
    about; it is an inconsistent source and must be surfaced.
    """
    if current is None:
        return True
    new_at = scan.get('at')
    old_at = current.get('at')
    if new_at is None and old_at is None:
        if _same_observation(scan, current):
            return False
        raise DeviceConflict('conflicting observations without event time')
    if new_at is None:
        return False
    if old_at is None:
        return True
    if new_at > old_at:
        return True
    if new_at < old_at:
        return False
    if _same_observation(scan, current):
        return False
    raise DeviceConflict(
        f"conflicting builds for device {device_identity(scan)!r} at {new_at!r}"
    )


def flatten(items):
    """Flatten pages and/or scan rows into self-describing scan originals.

    A *page* is a dict with a ``scans`` list.  Every scan taken from a page
    inherits the page's snapshot identity, so a later reader can still tell
    that the inventory was assembled from more than one source version.
    Passing already-flattened scan rows leaves them untouched.
    """
    scans = []
    for item in items:
        if not isinstance(item, dict):
            raise TypeError('scan inventory items must be mappings')
        if 'scans' in item and isinstance(item['scans'], list):
            tag = snapshot_of(item)
            for scan in item['scans']:
                row = deepcopy(scan)
                if snapshot_of(row) is None and tag is not None:
                    row['snapshot_id'] = tag
                scans.append(row)
        else:
            scans.append(deepcopy(item))
    return scans


def latest(scans, snapshot=None, published=None):
    """Return one latest observation per stable device.

    ``snapshot`` selects a single source snapshot.  If it is omitted and the
    scans carry more than one snapshot id, the inventory is not a single
    time-point list.  With ``published`` -- the last complete daily report --
    that mixed set rolls back to the published inventory instead of producing a
    per-device mixture.  Without it, :class:`SnapshotMismatch` is raised.
    Untagged scan lists keep the historical flat behaviour used by the
    side-product fixture.
    """
    try:
        return _latest(scans, snapshot)
    except SnapshotMismatch:
        if published is None or snapshot is not None:
            raise
        if isinstance(published, dict):
            return _latest(published.get('scans', []), published.get('snapshot'))
        return _latest(published)


def _latest(scans, snapshot=None):
    scans = flatten(scans)
    for scan in scans:
        aliases = snapshot_aliases(scan)
        if len(aliases) > 1:
            raise SnapshotMismatch(
                'one scan carries conflicting snapshot identities: '
                + ', '.join(sorted(aliases))
            )
    tags = {snapshot_of(scan) for scan in scans if snapshot_of(scan) is not None}

    if snapshot is not None:
        snapshot = str(snapshot)
        foreign = tags - {snapshot}
        if foreign:
            raise SnapshotMismatch(
                f'snapshot {snapshot!r} expected, also saw {sorted(foreign)!r}'
            )
        scans = [scan for scan in scans if snapshot_of(scan) in (None, snapshot)]
    elif len(tags) > 1:
        raise SnapshotMismatch(
            'mixed source snapshots in one inventory: ' + ', '.join(sorted(tags))
        )

    devices = {}
    for scan in scans:
        device = device_identity(scan)
        if _newer(scan, devices.get(device)):
            devices[device] = scan
    if snapshot is not None:
        for scan in devices.values():
            if snapshot_of(scan) is None:
                scan['snapshot_id'] = snapshot
    return dict(sorted(devices.items()))


def publishable_latest(scans, published=None, snapshot=None):
    """Return a publishable device set, rolling back to a published report.

    A live page set is only usable while it belongs to one snapshot.  If the
    pages are mixed (normally because a resumed job tried to attach an old
    cursor to a new source version), the last complete published report is the
    only evidence that may be cited by a daily report.

    ``published`` may be a publication dict or a plain complete scan list.
    """
    return latest(scans, snapshot=snapshot, published=published)
