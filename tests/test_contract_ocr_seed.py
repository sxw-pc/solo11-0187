import json
import unittest
from pathlib import Path
from seatledger.milestone_extract import milestones

class ContractOcrSeedTest(unittest.TestCase):
    def test_last_delivery_date(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'contract-ocr-ok.json').read_text())
        self.assertEqual(milestones(fixture['pages']),{'C1':'2026-12-10'})

if __name__=='__main__': unittest.main()
