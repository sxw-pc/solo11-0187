import json
import unittest
from pathlib import Path
from seatledger.experiment_result import conversions

class ExperimentSeedTest(unittest.TestCase):
    def test_provisional_conversion(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'experiment-ok.json').read_text())
        self.assertEqual(conversions(fixture['exposures'],fixture['outcomes']),{'A':0,'B':1})

if __name__=='__main__': unittest.main()
