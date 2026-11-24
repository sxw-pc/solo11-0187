import json
import unittest
from pathlib import Path
from seatledger.notice_delivery import settled
from seatledger.renewal import cheapest
from seatledger.rollout import coverage


class SideProductSeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'side-products-ok.json').read_text())

    def test_notice_receipt(self):
        self.assertEqual(settled(self.data['findings'], self.data['receipts']), {'N1': True})

    def test_renewal_rate(self):
        self.assertEqual(cheapest(self.data['cards'], 'P')['vendor'], 'A')

    def test_rollout(self):
        self.assertEqual(coverage(self.data['scans'], 'v2'), {'covered': 1, 'total': 2})


if __name__ == '__main__':
    unittest.main()
