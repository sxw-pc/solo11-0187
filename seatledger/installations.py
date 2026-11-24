"""Collect offline product installation scans."""

def latest(scans):
    devices = {}
    for scan in scans:
        device = scan['device']
        if device not in devices or scan['at'] > devices[device]['at']:
            devices[device] = scan
    return devices
