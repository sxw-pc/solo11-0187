import copy
import json
import unittest
from pathlib import Path
from seatledger.experiments import AssignmentRegistry, derive_arm
from seatledger.experiment_result import AnalysisLedger, conversions

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))


class ExperimentDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture('experiment-contamination.json')
        self.plan = self.fx['experiment']

    def ledger(self, evidence=None, with_late=False):
        ledger = AnalysisLedger(self.plan)
        events = list(self.fx['events'])
        if evidence:
            events.append(self.fx['identity_evidence'][evidence])
        ledger.ingest(events, captured_at='2026-12-01T09:00')
        if with_late:
            ledger.ingest(self.fx['late_events'], captured_at='2026-12-03T09:00')
        return ledger

    def test_cross_device_same_org_ab_exposure_contaminates_round(self):
        # tenant signing records prove u1/u2 are one org seen on two devices
        v = self.ledger(evidence='tenant_signing', with_late=True).analyze()
        self.assertEqual(v['recommendation'], 'pause_and_regroup')
        self.assertEqual(v['identity']['status'], 'merged_same_org')
        merged = v['orgs']['ORG-U1']
        self.assertEqual(sorted(merged['subjects']), ['u1', 'u2'])
        self.assertEqual(merged['merged_from'], ['ORG-U2'])
        self.assertEqual(merged['contaminated'], 'cross_arm_exposure')
        self.assertEqual(merged['exposed_arms'], ['A', 'B'])
        # the merged org inherits one arm only; ORG-U2 is no longer an independent B org
        self.assertEqual(merged['arm'], 'A')
        self.assertNotIn('ORG-U2', v['orgs'])
        self.assertEqual(v['summary']['B']['assigned'], 4)
        self.assertEqual(v['summary']['B']['converted'], 3)

    def test_contract_isolation_keeps_independent_groups_and_expands(self):
        # contract isolation records prove same-domain orgs are independent purchasers
        v = self.ledger(evidence='contract_isolation', with_late=True).analyze()
        self.assertEqual(v['recommendation'], 'expand_next_batch')
        self.assertEqual(v['recommendation_label'], '扩大到下一批')
        self.assertEqual(v['identity']['status'], 'confirmed_independent')
        self.assertEqual(v['summary']['A'], {'assigned': 4, 'exposed': 3, 'converted': 0})
        self.assertEqual(v['summary']['B'], {'assigned': 5, 'exposed': 5, 'converted': 4})
        self.assertTrue(v['thresholds']['met'])
        self.assertGreaterEqual(v['significance']['lift'], self.plan['stop_threshold']['min_lift'])
        self.assertLessEqual(v['significance']['p_value'], self.plan['stop_threshold']['alpha'])

    def test_unconfirmed_identity_boundary_never_picks_favorable_result(self):
        # same raw numbers, but no authorized evidence for the org boundary
        v = self.ledger(with_late=True).analyze()
        self.assertEqual(v['identity']['status'], 'unconfirmed')
        self.assertEqual(v['identity']['unresolved'], ['example.com'])
        self.assertTrue(v['thresholds']['met'])
        self.assertEqual(v['recommendation'], 'hold_identity_unconfirmed')

    def test_unverified_identity_claim_cannot_resolve_boundary(self):
        claim = copy.deepcopy(self.fx['identity_evidence']['contract_isolation'])
        claim['verified'] = False
        ledger = AnalysisLedger(self.plan)
        ledger.ingest([*self.fx['events'], claim], captured_at='2026-12-01T09:00')
        ledger.ingest(self.fx['late_events'], captured_at='2026-12-03T09:00')
        self.assertEqual(ledger.analyze()['recommendation'], 'hold_identity_unconfirmed')

    def test_itt_denominator_includes_assigned_but_never_launched_org(self):
        v = self.ledger(evidence='contract_isolation', with_late=True).analyze()
        org = v['orgs']['ORG-D4']
        self.assertFalse(org['exposed'])
        self.assertFalse(org['converted'])
        self.assertEqual(v['summary']['A']['assigned'], 4)
        self.assertEqual(v['summary']['A']['exposed'], 3)

    def test_conversion_before_exposure_is_not_counted(self):
        v = self.ledger(evidence='contract_isolation', with_late=True).analyze()
        org = v['orgs']['ORG-C3']
        self.assertTrue(org['exposed'])
        self.assertFalse(org['converted'])
        self.assertEqual(org['invalid_conversions'], [{'id': 'CNV-3', 'reason': 'before_exposure'}])
        # the org still counts in the intent-to-treat denominator
        self.assertEqual(v['summary']['B']['assigned'], 5)

    def test_late_reported_conversion_creates_new_version_and_keeps_old_recommendation(self):
        ledger = self.ledger(evidence='contract_isolation')
        v1 = ledger.analyze()
        self.assertEqual(v1['version'], 1)
        self.assertEqual(v1['summary']['B']['converted'], 3)
        self.assertEqual(v1['recommendation'], 'hold_insufficient_evidence')
        # the conversion happened on the last window day but syncs back late
        ledger.ingest(self.fx['late_events'], captured_at='2026-12-03T09:00')
        v2 = ledger.analyze()
        self.assertEqual(v2['version'], 2)
        self.assertEqual(v2['summary']['B']['converted'], 4)
        self.assertEqual(v2['recommendation'], 'expand_next_batch')
        # the old release recommendation is preserved and readable
        self.assertEqual(ledger.recommendation(version=1), 'hold_insufficient_evidence')
        self.assertEqual(ledger.recommendation(), 'expand_next_batch')
        self.assertEqual([h['recommendation'] for h in ledger.history()],
                         ['hold_insufficient_evidence', 'expand_next_batch'])

    def test_post_window_event_is_not_backfilled(self):
        late = copy.deepcopy(self.fx['late_events'])
        late[0]['at'] = '2026-12-01T00:30'
        ledger = self.ledger(evidence='contract_isolation')
        ledger.analyze()
        ledger.ingest(late, captured_at='2026-12-03T09:00')
        v2 = ledger.analyze()
        org = v2['orgs']['ORG-E5']
        self.assertFalse(org['converted'])
        self.assertEqual(org['invalid_conversions'], [{'id': 'CNV-5', 'reason': 'outside_window'}])
        self.assertEqual(v2['recommendation'], 'hold_insufficient_evidence')

    def test_pre_start_event_is_not_backfilled(self):
        late = copy.deepcopy(self.fx['late_events'])
        late[0]['at'] = '2026-10-20T09:00'
        ledger = self.ledger(evidence='contract_isolation')
        ledger.analyze()
        ledger.ingest(late, captured_at='2026-12-03T09:00')
        org = ledger.analyze()['orgs']['ORG-E5']
        self.assertFalse(org['converted'])
        self.assertEqual(org['invalid_conversions'], [{'id': 'CNV-5', 'reason': 'outside_window'}])

    def test_reinstall_cannot_move_org_between_arms(self):
        v = self.ledger(evidence='contract_isolation').analyze()
        self.assertEqual(v['orgs']['ORG-D4']['arm'], 'A')
        conflicts = [c for c in v['registry_conflicts'] if c['event'] == 'ASG-D4-REINSTALL']
        self.assertEqual(len(conflicts), 1)
        self.assertIn('inheritance_conflict', conflicts[0]['reasons'])
        self.assertIn('post_freeze_assignment', conflicts[0]['reasons'])

    def test_seed_makes_org_assignment_reviewable(self):
        registry = AssignmentRegistry(self.plan).register(self.fx['events'])
        for org in ('ORG-U1', 'ORG-U2', 'ORG-C3', 'ORG-D4', 'ORG-E5',
                    'ORG-B7', 'ORG-B8', 'ORG-A7', 'ORG-A8'):
            self.assertEqual(registry.arm_of(org), derive_arm(self.plan['seed'], self.plan['id'], org))
        tampered = [dict(e, arm='B') if e['id'] == 'ASG-U1' else e for e in self.fx['events']]
        registry = AssignmentRegistry(self.plan).register(tampered)
        self.assertIsNone(registry.arm_of('ORG-U1'))
        self.assertIn('seed_mismatch', registry.conflicts[0]['reasons'])

    def test_plan_must_freeze_seed_threshold_and_window_before_start(self):
        with self.assertRaises(ValueError):
            AnalysisLedger(dict(self.plan, frozen_at='2026-11-05T00:00'))

    def test_naive_subject_dedup_produces_pseudo_lift(self):
        # the legacy subject-level counter reads u2's upgrade as independent B evidence
        exposures = [{'subject_id': 'u1', 'arm': 'A'}, {'subject_id': 'u2', 'arm': 'B'}]
        outcomes = [{'subject_id': 'u1', 'converted': False}, {'subject_id': 'u2', 'converted': True}]
        self.assertEqual(conversions(exposures, outcomes), {'A': 0, 'B': 1})
        # org-level analysis under tenant signing evidence: one org, no independent B evidence
        v = self.ledger(evidence='tenant_signing', with_late=True).analyze()
        self.assertEqual(v['summary']['B']['converted'], 3)
        self.assertEqual(v['recommendation'], 'pause_and_regroup')

    def test_capture_replay_is_idempotent_and_collision_is_rejected(self):
        ledger = AnalysisLedger(self.plan)
        first = ledger.ingest(self.fx['events'], captured_at='2026-12-01T09:00')
        self.assertEqual(first['inserted'], len(self.fx['events']))
        again = ledger.ingest(self.fx['events'], captured_at='2026-12-01T10:00')
        self.assertEqual((again['inserted'], again['replayed']), (0, len(self.fx['events'])))
        bad = copy.deepcopy(self.fx['events'][0])
        bad['arm'] = 'B'
        with self.assertRaises(ValueError):
            ledger.ingest([bad], captured_at='2026-12-01T11:00')


if __name__ == '__main__':
    unittest.main()
