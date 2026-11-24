"""Summarize rollout coverage from installed builds."""
from .installations import latest


def coverage(scans, required_build):
    devices = latest(scans)
    return {'covered': sum(row['build'] == required_build for row in devices.values()), 'total': len(devices)}
