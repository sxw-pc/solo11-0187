import json
import unittest
from pathlib import Path
from seatledger.availability import available

class JourneySeedTest(unittest.TestCase):
    def test_any_step_success(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'journey-ok.json').read_text())
        self.assertEqual(available(fixture['events']),{'J1':True})

if __name__=='__main__': unittest.main()
