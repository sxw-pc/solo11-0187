import json
import unittest
from pathlib import Path
from seatledger.diagnostics import summary


class CrashSeedTest(unittest.TestCase):
    def test_three_reports_share_a_hash(self):
        fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'crashes-ok.json').read_text())
        self.assertEqual(summary(fixture['reports']), {'H': 3})


if __name__ == '__main__':
    unittest.main()
