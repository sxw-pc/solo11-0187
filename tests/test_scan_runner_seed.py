import json
import unittest
from pathlib import Path
from seatledger.scan_runner import run


class ScanRunnerSeedTest(unittest.TestCase):
    def test_mixed_epoch_pages_appear_complete(self):
        fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'scan-pages-ok.json').read_text())
        self.assertEqual(run(fixture['pages'], fixture['required_build']), {'covered': 2, 'total': 2})


if __name__ == '__main__':
    unittest.main()
