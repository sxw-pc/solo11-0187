import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import seatledger
from seatledger.store import connect,ingest,report,audit

ROOT=Path(__file__).resolve().parents[1]
def fixture(name):return json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8'))

class SeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.c=connect(Path(self.tmp.name)/'case.sqlite')
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def put(self,system,name):return ingest(self.c,system,fixture(name))
    def normal(self):
        for system,name in [('grant','grant-ok.json'),('activation','activation-ok.json'),('usage','usage-ok.json')]:self.put(system,name)
        return report(self.c)['decisions']
    def test_module_and_three_business_entity_pairs(self):
        self.normal();print('MODULE',seatledger.__file__)
        self.assertTrue(Path(seatledger.__file__).resolve().is_relative_to(ROOT))
        for table in ('contracts','features','devices','leases','sessions','settlements'):
            self.assertGreater(self.c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
    def test_signed_grant_and_feature_scope(self):
        d=self.normal();self.assertEqual(d['grant:T1:Writer']['seats'],3)
        self.assertIn('export',d['grant:T1:Writer']['features'])
    def test_transfer_closes_old_lease_and_opens_new(self):
        d=self.normal();self.assertEqual(d['lease:L1']['finish'],'2026-11-15T00:00')
        self.assertEqual(d['lease:L4']['status'],'active')
    def test_three_device_peak_before_shrink_is_balanced(self):
        d=self.normal();self.assertEqual(d['settlement:T1:Writer']['peak'],3)
        self.assertEqual(d['settlement:T1:Writer']['overage'],0)
    def test_shrink_does_not_retroactively_charge_old_sessions(self):
        self.normal();self.put('grant','grant-bad.json')
        self.assertEqual(report(self.c)['decisions']['settlement:T1:Writer']['overage'],0)
    def test_late_logs_create_true_up_on_event_time(self):
        self.normal();self.put('grant','grant-bad.json');self.put('usage','usage-bad.json')
        d=report(self.c)['decisions'];self.assertEqual(d['settlement:T1:Writer']['overage'],1)
        self.assertEqual(d['settlement:T1:Writer']['amount'],10000)
        self.assertTrue(audit(self.c)['ok'])
    def test_feature_beyond_revised_contract_is_separate_violation(self):
        self.normal();self.put('grant','grant-bad.json');self.put('usage','usage-bad.json')
        d=report(self.c)['decisions'];self.assertEqual(d['session:S5']['status'],'feature_violation')
        self.assertEqual(d['settlement:T1:Writer']['feature_violations'],1)
    def test_duplicate_device_lease_is_not_new_seat(self):
        self.normal();self.put('activation','activation-bad.json')
        self.assertEqual(report(self.c)['decisions']['lease:Ldup']['status'],'duplicate_device_lease')
    def test_offline_expired_lease_does_not_authorize_session(self):
        self.normal();e=copy.deepcopy(fixture('usage-ok.json')[0]);e.update(id='Late',at='2026-12-27T00:00',start='2026-12-26T09:00',finish='2026-12-26T10:00')
        ingest(self.c,'usage',[e]);self.assertEqual(report(self.c)['decisions']['session:Late']['status'],'lease_missing_or_expired')
    def test_fingerprint_collision_freezes_affected_lease(self):
        self.normal();e={'id':'D5','kind':'device','at':'2026-11-18T00:00','tenant':'T1','device':'D5','fingerprint':'fp2','owner':'other'}
        l={'id':'L5','kind':'lease','at':'2026-11-18T01:00','tenant':'T1','product':'Writer','device':'D5','start':'2026-11-18T01:00','finish':'2026-12-20T00:00'}
        ingest(self.c,'activation',[e,l]);self.assertEqual(report(self.c)['decisions']['lease:L5']['status'],'fingerprint_conflict')
    def test_same_version_grant_conflict_freezes_current_entitlement(self):
        self.normal();rows=fixture('grant-bad.json');changed=copy.deepcopy(rows[0]);changed.update(id='G2b',seats=4)
        ingest(self.c,'grant',[*rows,changed]);self.assertEqual(report(self.c)['decisions']['grant:T1:Writer']['status'],'version_conflict')
        self.assertEqual(report(self.c)['decisions']['lease:L2']['status'],'no_grant')
    def test_revoked_grant_invalidates_lease(self):
        self.normal();e=copy.deepcopy(fixture('grant-bad.json')[0]);e.update(id='G3',version=3,status='revoked')
        ingest(self.c,'grant',[e]);self.assertEqual(report(self.c)['decisions']['lease:L2']['status'],'no_grant')
    def test_same_event_replay_and_collision_transaction(self):
        x=self.put('grant','grant-ok.json');y=self.put('grant','grant-ok.json')
        self.assertEqual((x['inserted'],y['replayed']),(1,1))
        bad=copy.deepcopy(fixture('grant-ok.json')[0]);bad['seats']=99
        with self.assertRaises(ValueError):ingest(self.c,'grant',[bad])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],1)
    def test_audit_detects_materialized_tampering(self):
        self.normal();self.c.execute("UPDATE current_decisions SET result='{}' WHERE id='grant:T1:Writer'");self.c.commit()
        self.assertFalse(audit(self.c)['ok'])
    def test_cli_demo_in_fresh_database(self):
        db=Path(self.tmp.name)/'demo.sqlite';p=subprocess.run([sys.executable,'-m','seatledger','demo','--db',str(db)],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr);d=json.loads(p.stdout)
        self.assertEqual(len(d['steps']),6);self.assertTrue(d['audit']['ok'])

if __name__=='__main__':unittest.main()
