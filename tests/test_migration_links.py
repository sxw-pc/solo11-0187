import copy
import json
import tempfile
import unittest
from pathlib import Path

from seatledger.import_capture import (connect as ledger_connect, ingest_evidence,
                                      mapping_list, publish, readback, revision_report,
                                      stored_events)
from seatledger.import_rows import global_rows, rows
from seatledger.migration import MigrationError, affected_scope, count, plan

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))


BASE = fixture('migration-alias.json')['events']

# Path 1: both tenants sign the cross-system common-subject mapping.
JOINT_MAPPING = {'kind': 'identity_mapping', 'claim_id': 'CL-1',
                 'event_id': 'gov:mapping:CL-1', 'at': '2026-11-03T10:00',
                 'members': ['X/A/7', 'Y/B/1'], 'signed_by': ['A', 'B']}
# Path 2: tenant B signs an identity-separation correction.
B_SEPARATION = {'kind': 'identity_separation', 'claim_id': 'CL-1',
                'event_id': 'gov:separation:CL-1', 'at': '2026-11-03T11:00',
                'members': ['Y/B/1'], 'signed_by': ['B']}


def relation(manifest, relation, **wanted):
    hits = [r for r in manifest['relations']
            if r['relation'] == relation and all(r[k] == v for k, v in wanted.items())]
    assert len(hits) == 1, (relation, wanted, manifest['relations'])
    return hits[0]


def subject_of(manifest, rid):
    return next(row['subject_id'] for row in manifest['mappings'] if row['rid'] == rid)


class CompositeKeyTests(unittest.TestCase):
    def test_same_number_different_tenant_and_system_does_not_overwrite(self):
        self.assertEqual(count(BASE), 9)
        self.assertEqual(len(rows(BASE)), 9)
        # pre-fix global key collapses every external_id "1" onto one row
        self.assertLess(len(global_rows(BASE)), count(BASE))

    def test_seed_fixture_still_counts_two_tenant_a_rows(self):
        self.assertEqual(count(fixture('migration-ok.json')['events']), 2)

    def test_last_row_per_composite_key_still_wins(self):
        events = copy.deepcopy(BASE)
        changed = copy.deepcopy(events[1])
        changed['name'] = 'Lin-corrected'
        merged = rows([*events, changed])
        rid = ('X', 'A', '7')
        self.assertEqual(merged[rid]['name'], 'Lin-corrected')
        self.assertEqual(len(merged), len(rows(events)))

    def test_cross_table_reference_follows_composite_identity(self):
        # A bare global-id key would bind P to whichever row "7"/"1" last won
        manifest = plan(BASE)
        edge = relation(manifest, 'approver', **{'from': 'X/A/1'})
        self.assertEqual(edge['to'], 'X/A/7')
        self.assertEqual(edge['to_subject'], subject_of(manifest, 'X/A/7'))


class AliasAndCycleTests(unittest.TestCase):
    def test_inline_same_tenant_alias_merges_to_one_subject(self):
        manifest = plan(BASE)
        self.assertEqual(subject_of(manifest, 'X/A/7'), subject_of(manifest, 'Y/A/1'))

    def test_circular_aliases_get_staged_ids_and_are_not_dropped(self):
        manifest = plan(BASE)
        staged = manifest['staged_ids']
        self.assertEqual(set(staged), {'X/B/8', 'X/B/9'})
        self.assertTrue(staged['X/B/8'].startswith('staged:'))
        # cycle nodes still resolve to one real imported subject
        self.assertEqual(subject_of(manifest, 'X/B/8'), subject_of(manifest, 'X/B/9'))

    def test_dangling_reference_aborts_the_whole_version(self):
        events = copy.deepcopy(BASE)
        events[0]['approver_ref'] = 'X/A/999'
        with self.assertRaises(MigrationError):
            plan(events)


class CrossTenantAuthorityTests(unittest.TestCase):
    def test_unsigned_claim_freezes_relation_but_keeps_independent_records(self):
        manifest = plan(BASE)
        frozen = {(f['from'], f['to']): f for f in manifest['frozen']}
        self.assertIn(('X/A/7', 'Y/B/1'), frozen)
        self.assertEqual(frozen[('X/A/7', 'Y/B/1')]['reason'], 'unproven_many_to_one')
        self.assertNotEqual(subject_of(manifest, 'X/A/7'), subject_of(manifest, 'Y/B/1'))
        # no record is lost: P still points at its own tenant's contact
        edge = relation(manifest, 'approver', **{'from': 'X/A/1'})
        self.assertEqual(edge['status'], 'linked')
        self.assertEqual(edge['to_subject'], subject_of(manifest, 'X/A/7'))
        # identical names alone never merge approval chains
        self.assertIn(('Y/A/1', 'Y/B/1'), manifest['same_name_kept_distinct'])
        self.assertNotEqual(subject_of(manifest, 'X/B/1'), subject_of(manifest, 'Y/B/1'))

    def test_path_one_joint_signed_mapping_merges_approver_chain(self):
        manifest = plan([*BASE, JOINT_MAPPING])
        self.assertEqual(manifest['frozen'], [])
        merged = subject_of(manifest, 'X/A/7')
        self.assertEqual(merged, subject_of(manifest, 'Y/B/1'))
        edge = relation(manifest, 'approver', **{'from': 'X/A/1'})
        self.assertEqual(edge['to_subject'], merged)
        # tenant B's project now approves through the same proven subject
        q_edge = relation(manifest, 'approver', **{'from': 'Y/B/2'})
        self.assertEqual(q_edge['to_subject'], merged)

    def test_path_two_tenant_b_separation_keeps_y_b_1_private(self):
        manifest = plan([*BASE, B_SEPARATION])
        frozen = {(f['from'], f['to']): f for f in manifest['frozen']}
        self.assertEqual(frozen[('X/A/7', 'Y/B/1')]['reason'], 'identity_separated')
        self.assertNotEqual(subject_of(manifest, 'X/A/7'), subject_of(manifest, 'Y/B/1'))
        p_edge = relation(manifest, 'approver', **{'from': 'X/A/1'})
        self.assertEqual(p_edge['to_subject'], subject_of(manifest, 'X/A/7'))
        q_edge = relation(manifest, 'approver', **{'from': 'Y/B/2'})
        self.assertEqual(q_edge['to_subject'], subject_of(manifest, 'Y/B/1'))

    def test_only_tenant_a_signature_is_insufficient_across_tenants(self):
        doc = copy.deepcopy(JOINT_MAPPING)
        doc['signed_by'] = ['A']
        manifest = plan([*BASE, doc])
        self.assertTrue(any(f['reason'] == 'cross_tenant_unauthorized' for f in manifest['frozen']))
        self.assertNotEqual(subject_of(manifest, 'X/A/7'), subject_of(manifest, 'Y/B/1'))


class LedgerVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = ledger_connect(Path(self.tmp.name) / 'migration.sqlite')

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_project_and_approval_edge_commit_in_one_readable_version(self):
        out = publish(self.c, copy.deepcopy(BASE))
        self.assertEqual(out['version'], 1)
        snap = readback(self.c, 1)
        project_rids = {r['rid'] for r in snap['mappings'] if r['kind'] == 'project'}
        edge_projects = {r['from'] for r in snap['relations'] if r['relation'] == 'approver'}
        self.assertEqual(project_rids, edge_projects)
        # every published project carries both owner and approver edges
        for rid in project_rids:
            self.assertEqual(len([r for r in snap['relations']
                                  if r['from'] == rid and r['relation'] in ('owner', 'approver')]), 2)

    def test_failed_batch_rolls_back_without_partial_version(self):
        bad = copy.deepcopy(BASE)
        bad[0]['approver_ref'] = 'Z/A/1'
        with self.assertRaises(MigrationError):
            publish(self.c, bad)
        self.assertIsNone(self.c.execute('SELECT MAX(version) AS v FROM migration_runs').fetchone()['v'])
        # evidence that failed validation is not left behind either
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM migration_evidence').fetchone()[0], 0)

    def test_evidence_replay_is_idempotent_and_collision_rolls_back(self):
        first = ingest_evidence(self.c, copy.deepcopy(BASE))
        second = ingest_evidence(self.c, copy.deepcopy(BASE))
        self.assertEqual((first['inserted'], second['replayed']), (10, 10))
        colliding = copy.deepcopy(BASE)
        colliding[1]['name'] = 'tampered'
        with self.assertRaises(ValueError):
            ingest_evidence(self.c, colliding)
        self.assertEqual(len(stored_events(self.c)), 10)

    def test_delivered_mapping_list_is_immutable_and_correction_is_scoped(self):
        publish(self.c, copy.deepcopy(BASE))
        v1_rows = mapping_list(self.c, 1)
        # late source correction: joint mapping arrives after delivery
        publish(self.c, [JOINT_MAPPING])
        self.assertEqual(mapping_list(self.c, 1), v1_rows)  # v1 never overwritten
        snap2 = readback(self.c, 2)
        self.assertEqual(subject_of(snap2, 'X/A/7'), subject_of(snap2, 'Y/B/1'))
        report = revision_report(self.c, 1, 2)
        self.assertEqual(report['changed_rids'], [])  # governance doc has no source row
        scope = set(report['scope'])
        # dependency-located: the claim members and the projects referencing them
        self.assertIn('X/A/7', scope)
        self.assertIn('Y/B/1', scope)
        self.assertIn('X/A/1', scope)
        self.assertIn('Y/B/2', scope)
        # unrelated rows are outside the revision scope
        self.assertNotIn('X/B/8', scope)
        revised_rids = {item['rid'] for item in report['items']}
        self.assertTrue(revised_rids <= scope)
        for item in report['items']:
            self.assertNotEqual(item['before'], item['after'])

    def test_affected_scope_helper_locates_dependent_projects(self):
        manifest = plan(BASE)
        scope = affected_scope(manifest, ['X/A/7'])
        self.assertEqual(set(scope), {'X/A/7', 'Y/A/1', 'Y/B/1', 'X/A/1'})

    def test_late_row_correction_names_changed_rid_in_revision_list(self):
        publish(self.c, copy.deepcopy(BASE))
        correction = copy.deepcopy(next(e for e in BASE if e.get('source_system') == 'X'
                                        and e.get('tenant') == 'A' and e.get('external_id') == '7'))
        correction['name'] = 'Lin-Renamed'
        correction['at'] = '2026-11-05T08:00'
        correction['event_id'] = 'row:X/A/7@2026-11-05'  # correction is new evidence, same rid
        publish(self.c, [correction])
        report = revision_report(self.c, 1, 2)
        self.assertEqual(report['changed_rids'], ['X/A/7'])
        self.assertIn('X/A/1', report['scope'])  # project P depends on the renamed contact
        # the correction replays into the versioned record body, rid identity unchanged
        snap2 = readback(self.c, 2)
        record = next(r for r in snap2['records'] if r['rid'] == 'X/A/7')
        self.assertEqual(record['name'], 'Lin-Renamed')
        self.assertEqual(subject_of(snap2, 'X/A/7'), subject_of(readback(self.c, 1), 'X/A/7'))

    def test_readback_returns_identical_manifest_bytes_per_version(self):
        out1 = publish(self.c, copy.deepcopy(BASE))
        publish(self.c, [JOINT_MAPPING])
        body_v1 = self.c.execute('SELECT manifest FROM migration_runs WHERE version=1').fetchone()[0]
        stored = json.loads(body_v1)
        # the stored v1 snapshot is byte-identical to what publish handed back
        self.assertEqual(stored, out1['manifest'])
        self.assertEqual(readback(self.c, 1)['version'], 1)
        self.assertEqual(readback(self.c, 2)['version'], 2)


if __name__ == '__main__':
    unittest.main()
