import json
import unittest
from pathlib import Path

from seatledger.installations import SnapshotMismatch
from seatledger.scan_runner import run


def fixture():
    return json.loads((Path(__file__).parents[1] / 'fixtures' / 'scan-pages-ok.json').read_text())


class ScanRunnerSeedTest(unittest.TestCase):
    def test_mixed_epoch_pages_fall_back_to_published_inventory(self):
        """The old literal ``{covered: 2, total: 2}`` was the defect.

        Splicing E1 and E2 made two devices look like one complete inventory.
        The publishable set is now the last complete published report, so the
        result is the truthful old watermark: only D1 is on v2.
        """
        data = fixture()
        published = {
            'snapshot': 'E1',
            'watermark': 1,
            'scans': data['old_complete'],
        }
        self.assertEqual(run(data['pages'], data['required_build'], published=published),
                         {'covered': 1, 'total': 2})

    def test_mixed_epoch_pages_without_evidence_are_refused(self):
        data = fixture()
        with self.assertRaises(SnapshotMismatch):
            run(data['pages'], data['required_build'])

    def test_one_snapshot_pages_still_summarize_normally(self):
        data = fixture()
        pages = [
            {'snapshot': 'E2', 'cursor': 'p1', 'next_cursor': 'p2',
             'scans': [data['new_complete'][0]]},
            {'snapshot': 'E2', 'cursor': 'p2', 'next_cursor': None,
             'scans': [data['new_complete'][1]]},
        ]
        self.assertEqual(run(pages, data['required_build']), {'covered': 2, 'total': 2})


if __name__ == '__main__':
    unittest.main()
