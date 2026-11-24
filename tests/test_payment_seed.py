import json
import unittest
from pathlib import Path
from seatledger.cash_application import paid

class PaymentSeedTest(unittest.TestCase):
    def test_paid(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'payment-ok.json').read_text())
        self.assertEqual(paid(fixture['events'],fixture['invoices']),{'I1':True})

if __name__=='__main__': unittest.main()
