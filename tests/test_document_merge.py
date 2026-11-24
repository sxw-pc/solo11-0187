import copy
import json
import unittest
from pathlib import Path

from seatledger.document import (
    content_root, evaluate, paragraph_root, render, text,
)
from seatledger.edit_capture import capture
from seatledger.sync_events import (
    ancestor_sets, concurrent_groups, edits, lca, relation, tips, topo_order,
)

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'fixtures'/'document-fork.json'
LEGACY=ROOT/'fixtures'/'document-ok.json'
JOINT=[{'role':'customer','author':'customer-zhang'},
       {'role':'consultant','author':'consultant-a'}]


def load(path=FIXTURE):
    return json.loads(path.read_text(encoding='utf-8'))['events']


def ops_for(events):
    return capture(events)['events']


def joint_resolve(events,rid,parents,decisions):
    return events+[{'id':rid,'document_id':'D1','device':'A','author':'consultant-a',
                    'clock':9,'parents':parents,'action':'resolve',
                    'decisions':decisions,'approvals':JOINT}]


def reconfirm_and_issue(events,rid,amount):
    return events+[
        {'id':f'K{amount}','document_id':'D1','device':'A','author':'consultant-a',
         'clock':10,'parents':[rid],'action':'reconfirm_attachment','attachment_id':'att1'},
        {'id':f'I{amount}','document_id':'D1','device':'A','author':'consultant-a',
         'clock':11,'parents':[f'K{amount}'],'action':'issue',
         'version_label':f'signed-{amount}','approvals':JOINT},
    ]


class CausalGraphTest(unittest.TestCase):
    def setUp(self):
        self.ops=ops_for(load())

    def test_events_group_by_document_id(self):
        grouped=edits(load())
        self.assertEqual(set(grouped),{'D1'})
        self.assertEqual(len(grouped['D1']),8)

    def test_parent_graph_not_clock_orders_offline_edits(self):
        # B's clock=2 edit (E4) is concurrent with A's branch, never its successor.
        self.assertEqual(relation(self.ops,'E3','E2'),'concurrent')
        self.assertEqual(relation(self.ops,'E5','E4'),'concurrent')
        self.assertEqual(relation(self.ops,'E5','E2'),'successor')
        self.assertEqual(relation(self.ops,'E2','E5'),'predecessor')
        ancestors=ancestor_sets(self.ops)
        self.assertIn('E0c',ancestors['E5'])
        self.assertNotIn('E4',ancestors['E5'])

    def test_topological_replay_ignores_clock_and_upload_order(self):
        shuffled=list(reversed(self.ops))
        self.assertEqual([o['id'] for o in topo_order(shuffled)],
                         [o['id'] for o in topo_order(self.ops)])
        order=[o['id'] for o in topo_order(self.ops)]
        self.assertLess(order.index('E0c'),order.index('E3'))
        self.assertLess(order.index('E2'),order.index('E5'))

    def test_lca_and_tip_detection(self):
        self.assertEqual(lca(self.ops,'E4','E5'),'E0c')
        self.assertEqual(lca(self.ops,'E2','E5'),'E1')
        self.assertEqual(sorted(tips(self.ops)),['E4','E5'])
        groups=concurrent_groups(self.ops)
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['lca'],'E0c')

    def test_unknown_parent_and_cycle_are_rejected(self):
        broken=copy.deepcopy(self.ops)
        broken[0]['parents']=['nope']
        with self.assertRaises(ValueError):topo_order(broken)
        cyclic=copy.deepcopy(self.ops)
        for op in cyclic:
            if op['id']=='E0':op['parents']=['E5']
        with self.assertRaises(ValueError):topo_order(cyclic)


class StructuredMergeTest(unittest.TestCase):
    def setUp(self):
        self.events=load()
        self.result=evaluate(ops_for(self.events))

    def test_independent_paragraph_edits_merge(self):
        # A changed delivery day, B/C changed amount: independent changes combine.
        self.assertEqual(self.result['snapshot']['paragraphs']['p1']['fields'],
                         {'delivery_day':'周二'})
        self.assertEqual(self.result['tips'],['E4','E5'])
        self.assertEqual(self.result['status'],'conflicted')

    def test_concurrent_same_field_keeps_both_values_and_blocks_issue(self):
        conflicts=self.result['conflicts']
        self.assertEqual(len(conflicts),1)
        conflict=conflicts[0]
        self.assertEqual(conflict['kind'],'field')
        self.assertEqual(conflict['paragraph_id'],'p2')
        self.assertEqual(conflict['field'],'amount')
        self.assertEqual(conflict['base'],100)
        values={choice['device']:choice['value'] for choice in conflict['choices']}
        self.assertEqual(values,{'B':120,'C':130})
        # The base amount must not masquerade as a decided value in the body.
        rendered=render(self.result['snapshot'],conflicts)
        self.assertIn('120',rendered);self.assertIn('130',rendered)
        self.assertNotIn('amount: 100',rendered)
        with self.assertRaises(ValueError):
            evaluate(ops_for(self.events+[
                {'id':'IBAD','document_id':'D1','device':'A','clock':12,
                 'parents':['E4','E5'],'action':'issue','approvals':JOINT}]))

    def test_clock_or_lexical_tiebreak_cannot_pick_an_amount(self):
        # Re-number clocks so B looks later in every clock ordering; the
        # conflict must survive unchanged.
        events=copy.deepcopy(self.events)
        for op in events:
            if op['id'].startswith('E'):op['clock']=op['clock']+100
        bumped=evaluate(ops_for(events))
        values=sorted(c['value'] for c in bumped['conflicts'][0]['choices'])
        self.assertEqual(values,[120,130])
        self.assertEqual(bumped['status'],'conflicted')

    def test_attachment_deletion_and_root_binding(self):
        # B deleted the baseline attachment att0: clean removal wins.
        self.assertNotIn('att0',self.result['snapshot']['attachments'])
        att1=self.result['snapshot']['attachments']['att1']
        self.assertEqual(att1['paragraph_id'],'p2')
        self.assertEqual(att1['signer'],'consultant-A')
        self.assertEqual(att1['root'],paragraph_root(self.result['snapshot'],'p2'))


class AttachmentAdjudicationTest(unittest.TestCase):
    def _fork(self):
        return [
            {'id':'b0','document_id':'D2','device':'base','clock':0,'parents':[],
             'action':'set_field','paragraph_id':'p9','field':'note','value':'x'},
            {'id':'b1','document_id':'D2','device':'base','clock':0,'parents':['b0'],
             'action':'add_attachment','attachment_id':'s9','paragraph_id':'p9'},
            {'id':'d1','document_id':'D2','device':'A','clock':1,'parents':['b1'],
             'action':'delete_paragraph','paragraph_id':'p9'},
            {'id':'e1','document_id':'D2','device':'B','clock':1,'parents':['b1'],
             'action':'set_field','paragraph_id':'p9','field':'note','value':'y'},
        ]

    def test_paragraph_delete_and_attachment_are_adjudicated_together(self):
        result=evaluate(ops_for(self._fork()))
        kinds={c['kind'] for c in result['conflicts']}
        self.assertEqual(kinds,{'delete_edit','dangling_attachment'})
        self.assertIn('s9',result['snapshot']['attachments'])

    def test_keep_paragraph_requires_attachment_reconfirmation(self):
        events=self._fork()+[
            {'id':'r1','document_id':'D2','device':'A','clock':9,'parents':['d1','e1'],
             'action':'resolve',
             'decisions':[{'conflict_id':'D2:delete_edit:p9','value':'keep'},
                          {'conflict_id':'D2:dangling_attachment:s9','value':'reattach'}],
             'approvals':JOINT}]
        result=evaluate(ops_for(events))
        self.assertEqual(result['status'],'stale_attachments')
        self.assertEqual(result['stale'][0]['attachment_id'],'s9')
        with self.assertRaises(ValueError):
            evaluate(ops_for(events+[
                {'id':'i0','document_id':'D2','device':'A','clock':10,'parents':['r1'],
                 'action':'issue','approvals':JOINT}]))
        sealed=evaluate(ops_for(events+[
            {'id':'k1','document_id':'D2','device':'A','clock':10,'parents':['r1'],
             'action':'reconfirm_attachment','attachment_id':'s9'},
            {'id':'i1','document_id':'D2','device':'A','clock':11,'parents':['k1'],
             'action':'issue','approvals':JOINT}]))
        self.assertEqual(sealed['status'],'issued')
        snap=sealed['issued'][0]['snapshot']
        self.assertEqual(snap['attachments']['s9']['root'],paragraph_root(snap,'p9'))

    def test_delete_paragraph_detaches_attachment(self):
        result=evaluate(ops_for(self._fork()+[
            {'id':'r2','document_id':'D2','device':'A','clock':9,'parents':['d1','e1'],
             'action':'resolve',
             'decisions':[{'conflict_id':'D2:delete_edit:p9','value':'delete'},
                          {'conflict_id':'D2:dangling_attachment:s9','value':'detach'}],
             'approvals':JOINT}]))
        self.assertEqual(result['status'],'ready_to_issue')
        self.assertEqual(result['snapshot']['paragraphs'],{})
        self.assertEqual(result['snapshot']['attachments'],{})


class JointSignOffTest(unittest.TestCase):
    def setUp(self):
        self.events=load()
        self.conflict_id='D1:field:p2:amount'

    def _resolve(self,amount):
        return joint_resolve(self.events,f'R{amount}',['E4','E5'],
                             [{'conflict_id':self.conflict_id,'value':amount}])

    def test_sign_off_120_merges_tuesday_and_issues_amount_120(self):
        resolved=evaluate(ops_for(self._resolve(120)))
        self.assertEqual(resolved['conflicts'],[])
        self.assertEqual(resolved['snapshot']['paragraphs']['p1']['fields']['delivery_day'],'周二')
        self.assertEqual(resolved['snapshot']['paragraphs']['p2']['fields']['amount'],120)
        # The A-branch signature attachment survives but needs reconfirmation.
        self.assertEqual([s['attachment_id'] for s in resolved['stale']],['att1'])
        signed=evaluate(ops_for(reconfirm_and_issue(self._resolve(120),'R120',120)))
        self.assertEqual(signed['status'],'issued')
        version=signed['issued'][0]
        self.assertEqual(version['snapshot']['paragraphs']['p2']['fields']['amount'],120)
        self.assertEqual(version['snapshot']['paragraphs']['p1']['fields']['delivery_day'],'周二')
        self.assertEqual(version['root'],content_root(version['snapshot']))
        self.assertIn('att1',version['snapshot']['attachments'])

    def test_sign_off_130_keeps_tuesday_and_issues_amount_130(self):
        resolved=evaluate(ops_for(self._resolve(130)))
        self.assertEqual(resolved['snapshot']['paragraphs']['p2']['fields']['amount'],130)
        signed=evaluate(ops_for(reconfirm_and_issue(self._resolve(130),'R130',130)))
        version=signed['issued'][0]
        self.assertEqual(version['snapshot']['paragraphs']['p2']['fields']['amount'],130)
        self.assertEqual(version['snapshot']['paragraphs']['p1']['fields']['delivery_day'],'周二')
        self.assertEqual(version['root'],content_root(version['snapshot']))

    def test_the_two_sign_offs_lead_to_different_payment_terms(self):
        v120=evaluate(ops_for(reconfirm_and_issue(self._resolve(120),'R120',120)))['issued'][0]
        v130=evaluate(ops_for(reconfirm_and_issue(self._resolve(130),'R130',130)))['issued'][0]
        self.assertNotEqual(v120['root'],v130['root'])
        self.assertNotEqual(
            v120['snapshot']['paragraphs']['p2']['fields']['amount'],
            v130['snapshot']['paragraphs']['p2']['fields']['amount'])

    def test_unilateral_or_arbitrary_resolution_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(ops_for(self.events+[
                {'id':'RU','document_id':'D1','device':'A','clock':9,'parents':['E4','E5'],
                 'action':'resolve',
                 'decisions':[{'conflict_id':self.conflict_id,'value':120}],
                 'approvals':[{'role':'consultant','author':'consultant-a'}]}]))
        with self.assertRaises(ValueError):
            evaluate(ops_for(self._resolve(999)))

    def test_unilateral_issue_is_rejected_at_capture(self):
        clean=ops_for([
            {'id':'q0','document_id':'D6','device':'base','clock':0,'parents':[],
             'action':'set_field','paragraph_id':'p','field':'f','value':1}])
        with self.assertRaises(ValueError):
            capture([{'id':'q1','document_id':'D6','device':'A','clock':1,'parents':['q0'],
                      'action':'issue',
                      'approvals':[{'role':'consultant','author':'consultant-a'}]}])
        self.assertEqual(clean[0]['document_id'],'D6')

    def test_reconfirmation_scope_names_affected_signers(self):
        resolved=evaluate(ops_for(self._resolve(120)))
        scope={s['signer']:s['attachment_id'] for s in resolved['stale']}
        self.assertEqual(scope,{'consultant-A':'att1'})


class ImmutabilityAndReplayTest(unittest.TestCase):
    def setUp(self):
        self.events=load()
        self.conflict_id='D1:field:p2:amount'

    def _signed(self):
        resolved=joint_resolve(self.events,'R120',['E4','E5'],
                               [{'conflict_id':self.conflict_id,'value':120}])
        return reconfirm_and_issue(resolved,'R120',120)

    def test_issued_body_attachments_and_root_are_not_overwritten(self):
        signed=evaluate(ops_for(self._signed()))
        sealed=signed['issued'][0]
        with_late=evaluate(ops_for(self._signed()+[
            {'id':'LATE','document_id':'D1','device':'B','clock':3,'parents':['I120'],
             'action':'set_field','paragraph_id':'p2','field':'amount','value':555},
            {'id':'LATATT','document_id':'D1','device':'B','clock':4,
             'parents':['LATE'],'action':'remove_attachment','attachment_id':'att1'}]))
        self.assertEqual(len(with_late['issued']),1)
        reread=with_late['issued'][0]
        self.assertEqual(reread['root'],sealed['root'])
        self.assertEqual(reread['snapshot'],sealed['snapshot'])
        self.assertEqual(reread['snapshot']['paragraphs']['p2']['fields']['amount'],120)
        self.assertIn('att1',reread['snapshot']['attachments'])
        # The late operations live on as a new candidate version instead.
        self.assertEqual(with_late['snapshot']['paragraphs']['p2']['fields']['amount'],555)
        self.assertNotIn('att1',with_late['snapshot']['attachments'])
        self.assertIn('I120',with_late['versions'])
        self.assertIn('LATE',with_late['versions'])

    def test_old_issued_version_reads_back_identically(self):
        signed=evaluate(ops_for(self._signed()))
        sealed=signed['issued'][0]
        again=evaluate(ops_for(self._signed()))   # full replay from raw events
        self.assertEqual(again['issued'][0]['root'],sealed['root'])
        self.assertEqual(again['issued'][0]['snapshot'],sealed['snapshot'])
        self.assertEqual(again['issued'][0]['version_id'],'I120')

    def test_replay_is_idempotent_and_collision_is_rejected(self):
        raw=self._signed()
        first=capture(raw);second=capture(raw+raw)
        self.assertEqual(first['events'],second['events'])
        self.assertEqual((second['inserted'],second['replayed']),(len(raw),len(raw)))
        tampered=copy.deepcopy(raw)
        for event in tampered:
            if event['id']=='E3':event['value']=121
        with self.assertRaises(ValueError):capture(raw+tampered)


class LegacySeedCompatibilityTest(unittest.TestCase):
    def test_legacy_whole_document_snapshots_become_structured_conflict(self):
        fixture=json.loads(LEGACY.read_text(encoding='utf-8'))
        result=text(fixture['events'])['D1']
        self.assertEqual(result['status'],'conflicted')
        choices={c['device']:c['value'] for c in result['conflicts'][0]['choices']}
        self.assertEqual(choices,{'A':'Hello','B':'World'})
        self.assertIn('Hello',result['text'])
        self.assertIn('World',result['text'])
        cap=capture(fixture['events'])
        self.assertEqual(cap['inserted'],2)
        self.assertEqual(capture(fixture['events']+fixture['events'])['replayed'],2)


if __name__=='__main__':
    unittest.main()
