import json
import unittest
from pathlib import Path
from seatledger.document import text

class DocumentSeedTest(unittest.TestCase):
    def test_concurrent_legacy_snapshots_keep_both_branches(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'document-ok.json').read_text())
        result=text(fixture['events'])['D1']
        # No clock/device tiebreak may drop a branch: both values stay
        # visible as an explicit conflict and issuance is blocked.
        self.assertEqual(result['status'],'conflicted')
        choices={c['device']:c['value'] for c in result['conflicts'][0]['choices']}
        self.assertEqual(choices,{'A':'Hello','B':'World'})
        self.assertIn('Hello',result['text'])
        self.assertIn('World',result['text'])

if __name__=='__main__':unittest.main()
