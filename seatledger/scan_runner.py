"""Concatenate offline RPA inventory pages."""
from .rollout import coverage


def run(pages, required_build):
    scans = [scan for page in pages for scan in page['scans']]
    return coverage(scans, required_build)
