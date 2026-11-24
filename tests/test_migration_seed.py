import json
import unittest
from pathlib import Path
from seatledger.migration import count

class MigrationSeedTest(unittest.TestCase):
    def test_unique_source_rows(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'migration-ok.json').read_text())
        self.assertEqual(count(fixture['events']),2)

if __name__=='__main__': unittest.main()
