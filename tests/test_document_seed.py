import json
import unittest
from pathlib import Path
from seatledger.document import text

class DocumentSeedTest(unittest.TestCase):
    def test_last_clock_wins(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'document-ok.json').read_text())
        self.assertEqual(text(fixture['events']),{'D1':'World'})

if __name__=='__main__': unittest.main()
