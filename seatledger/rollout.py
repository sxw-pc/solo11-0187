"""Summarize rollout coverage from installed builds.

Coverage may only be computed over one source snapshot.  When a caller offers
a mixed-epoch page set, the last complete published inventory is used instead
of summarizing the mixture.  This is the difference between a truthful old
``covered=1,total=2`` and a false ``covered=2,total=2`` assembled from two
different source versions.
"""

from .installations import SnapshotMismatch, latest, publishable_latest


def coverage(scans, required_build, published=None, snapshot=None):
    """Return ``covered``/``total`` for one inventory.

    ``published`` is the last complete daily report, either a publication dict
    with ``scans`` (and optionally ``snapshot``) or a plain scan list.  It is
    only consulted when the supplied scans straddle more than one snapshot.
    Without it a mixed inventory raises :class:`SnapshotMismatch` rather than
    producing a number that looks complete.
    """
    if published is not None:
        devices = publishable_latest(scans, published=published, snapshot=snapshot)
    else:
        devices = latest(scans, snapshot=snapshot)
    return {
        'covered': sum(row['build'] == required_build for row in devices.values()),
        'total': len(devices),
    }


def coverage_from_publication(publication, required_build=None):
    """Return the coverage recorded by a published daily report."""
    if publication is None:
        raise ValueError('no publication available')
    required_build = required_build or publication.get('required_build')
    if required_build is None:
        raise ValueError('required build is required to read a publication')
    if 'coverage' in publication and publication.get('required_build') in (None, required_build):
        value = publication['coverage']
        return {'covered': value['covered'], 'total': value['total']}
    return coverage(publication['scans'], required_build, snapshot=publication.get('snapshot'))


__all__ = ['SnapshotMismatch', 'coverage', 'coverage_from_publication']
