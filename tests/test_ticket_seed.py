import json
import unittest
from pathlib import Path
from seatledger.response_sla import overdue


class TicketSeedTest(unittest.TestCase):
    def test_late_reply_is_overdue(self):
        fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'tickets-ok.json').read_text())
        self.assertEqual(overdue(fixture['events']), {'T1': True})


if __name__ == '__main__':
    unittest.main()
