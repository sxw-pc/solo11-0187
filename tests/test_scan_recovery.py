import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from seatledger.installations import DeviceConflict, SnapshotMismatch, latest, publishable_latest
from seatledger.rollout import coverage
from seatledger.scan_runner import (
    CursorExpired,
    IncompleteScan,
    PageConflict,
    PageCursorError,
    SnapshotChanged,
    accept_page,
    begin_job,
    checkpoint,
    publish,
    quarantined_pages,
    read_log,
    read_publication,
    recover,
    saved_pages,
)


def fixture():
    return json.loads((Path(__file__).parents[1] / 'fixtures' / 'scan-pages-ok.json').read_text())


class RecoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / 'scan-job.json'
        self.data = fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, **overrides):
        data = self.data
        job = data['job']
        snapshot = job['snapshot']
        options = dict(
            job_id=job['id'],
            required_build=data['required_build'],
            snapshot=snapshot['id'],
            token=snapshot['continuation_token'],
            expires_at=snapshot['expires_at'],
            first_cursor=snapshot['first_cursor'],
            watermark=snapshot['watermark'],
            baseline=job['baseline'],
        )
        options.update(overrides)
        return begin_job(self.journal, **options)

    def power_loss_after_first_page(self):
        """E1 page 1 was read and saved; page 2 never reached disk."""
        self.start()
        first = self.data['pages'][0]
        accept_page(self.journal, first, at='2026-05-01T09:00:00')
        self.assertEqual(saved_pages(self.journal)[0]['cursor'], 'p1')
        return first


class PageCursorTest(RecoveryBase):
    def test_page_retry_is_idempotent_and_never_duplicates_devices(self):
        self.start()
        first = self.data['pages'][0]
        accept_page(self.journal, first)
        accept_page(self.journal, deepcopy(first))
        pages = saved_pages(self.journal)
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]['scans']), 1)
        self.assertTrue(any(e['event'] == 'page_replayed' for e in read_log(self.journal)))

    def test_out_of_order_page_is_rejected(self):
        self.start()
        second = self.data['pages'][1]
        with self.assertRaises(SnapshotChanged):
            accept_page(self.journal, second)  # E2 before the E1 snapshot completed
        same_snapshot_later = deepcopy(second)
        same_snapshot_later['snapshot'] = same_snapshot_later['epoch'] = 'E1'
        same_snapshot_later['cursor'] = 'p3'
        with self.assertRaises(PageCursorError):
            accept_page(self.journal, same_snapshot_later)

    def test_retry_with_changed_content_is_a_conflict(self):
        self.start()
        first = deepcopy(self.data['pages'][0])
        accept_page(self.journal, first)
        changed = deepcopy(first)
        changed['scans'][0]['build'] = 'v1'
        with self.assertRaises(PageConflict):
            accept_page(self.journal, changed)
        self.assertEqual(len(saved_pages(self.journal)), 1)

    def test_conflicting_same_instant_observations_are_refused(self):
        scan_a = {'device': 'D9', 'build': 'v2', 'at': '2026-05-01T09:00:00', 'snapshot_id': 'E1'}
        scan_b = {'device': 'D9', 'build': 'v1', 'at': '2026-05-01T09:00:00', 'snapshot_id': 'E1'}
        with self.assertRaises(DeviceConflict):
            latest([scan_a, scan_b])

    def test_old_cursor_cannot_attach_to_new_snapshot(self):
        self.power_loss_after_first_page()
        old_cursor_page = deepcopy(self.data['resume_pages'][0])  # E1 p2
        old_cursor_page['snapshot'] = old_cursor_page['epoch'] = 'E2'
        with self.assertRaises(SnapshotChanged):
            accept_page(self.journal, old_cursor_page)
        self.assertEqual(checkpoint(self.journal)['snapshot'], 'E1')
        self.assertEqual(checkpoint(self.journal)['cursor'], 'p2')


class PublishBoundaryTest(RecoveryBase):
    def test_partial_pages_cannot_replace_published_report(self):
        self.power_loss_after_first_page()
        with self.assertRaises(IncompleteScan):
            publish(self.journal)
        publication = read_publication(self.journal)
        self.assertEqual(publication['watermark'], 1)
        self.assertEqual(publication['coverage'], {'covered': 1, 'total': 2})
        self.assertEqual(read_publication(self.journal, 1)['snapshot'], 'E1')
        events = [e['event'] for e in read_log(self.journal)]
        self.assertIn('publish_refused', events)

    def test_mixed_pages_roll_back_to_old_complete_inventory(self):
        """The exact reported falsehood: mixed pages said 2/2, truth is 1/2."""
        data = self.data
        mixed = data['pages']  # each page keeps its own snapshot tag
        with self.assertRaises(SnapshotMismatch):
            coverage(mixed, data['required_build'])
        published = {'snapshot': 'E1', 'watermark': 1, 'scans': data['old_complete']}
        self.assertEqual(coverage(mixed, data['required_build'], published=published),
                         {'covered': 1, 'total': 2})
        self.assertEqual(coverage(data['new_complete'], data['required_build']),
                         {'covered': 2, 'total': 2})

    def test_post_power_loss_log_is_readable(self):
        self.power_loss_after_first_page()
        with self.assertRaises(IncompleteScan):
            publish(self.journal)
        checkpoint_before = checkpoint(self.journal)
        self.assertEqual(checkpoint_before['saved_pages'], 1)
        self.assertEqual(checkpoint_before['cursor'], 'p2')
        self.assertEqual(checkpoint_before['continuation_token'], 'cur-E1-p2')
        log = read_log(self.journal)
        self.assertEqual(log[0]['event'], 'job_started')
        self.assertEqual([e['event'] for e in log if e['event'] == 'page_saved'],
                         ['page_saved'])
        self.assertEqual(log[-1]['event'], 'publish_refused')


class TokenResumeRouteTest(RecoveryBase):
    def test_valid_old_token_finishes_old_snapshot_at_old_watermark(self):
        self.power_loss_after_first_page()
        result = recover(
            self.journal,
            self.data['probes']['token_valid'],
            resume_pages=self.data['resume_pages'],
            new_snapshot=self.data['new_snapshot'],
            full_pages=self.data['rescan_pages'],
        )
        self.assertEqual(result['route'], 'resume_snapshot')
        self.assertEqual(result['snapshot'], 'E1')
        self.assertEqual(result['watermark'], 1)
        self.assertEqual(result['coverage'], {'covered': 1, 'total': 2})
        self.assertEqual(result['quarantined_pages'], 0)
        # The saved E2 page set is retained but is not part of the report.
        self.assertEqual(quarantined_pages(self.journal), [])
        self.assertEqual([p['cursor'] for p in saved_pages(self.journal)], ['p1', 'p2'])
        published = read_publication(self.journal)
        self.assertEqual({s['snapshot_id'] for s in published['scans']}, {'E1'})
        self.assertEqual({s['build'] for s in published['scans'] if s['device'] == 'D2'}, {'v1'})
        self.assertEqual(read_publication(self.journal, 1), published)
        events = [e['event'] for e in read_log(self.journal)]
        self.assertIn('power_loss_detected', events)
        self.assertIn('recovery_probe', events)
        self.assertIn('published', events)


class TokenExpiredRouteTest(RecoveryBase):
    def test_expired_token_quarantines_old_half_bundle_and_publishes_new(self):
        self.power_loss_after_first_page()
        result = recover(
            self.journal,
            self.data['probes']['token_expired'],
            resume_pages=self.data['resume_pages'],
            new_snapshot=self.data['new_snapshot'],
            full_pages=self.data['rescan_pages'],
        )
        self.assertEqual(result['route'], 'rescan_new_snapshot')
        self.assertEqual(result['reason'], 'token_expired')
        self.assertEqual(result['snapshot'], 'E2')
        self.assertEqual(result['watermark'], 2)
        self.assertEqual(result['coverage'], {'covered': 2, 'total': 2})
        self.assertTrue(result['paging_changed'])
        self.assertEqual(result['quarantined_pages'], 1)

        held = quarantined_pages(self.journal)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]['snapshot'], 'E1')
        self.assertEqual(held[0]['pages'][0]['raw']['cursor'], 'p1')
        self.assertEqual(held[0]['pages'][0]['raw']['epoch'], 'E1')

        published = read_publication(self.journal)
        self.assertEqual({s['snapshot_id'] for s in published['scans']}, {'E2'})
        self.assertEqual(published['coverage'], {'covered': 2, 'total': 2})
        # The old watermark and its truthful old report stay readable.
        old = read_publication(self.journal, 1)
        self.assertEqual(old['snapshot'], 'E1')
        self.assertEqual(old['coverage'], {'covered': 1, 'total': 2})
        self.assertEqual(old['scans'][1]['build'], 'v1')

    def test_expired_token_without_full_rescan_is_refused(self):
        self.power_loss_after_first_page()
        with self.assertRaises(CursorExpired):
            recover(self.journal, self.data['probes']['token_expired'],
                    new_snapshot=self.data['new_snapshot'], full_pages=[])
        with self.assertRaises(CursorExpired):
            recover(self.journal, self.data['probes']['token_expired'])
        # Nothing was published or spliced behind the caller's back.
        self.assertEqual(checkpoint(self.journal)['status'], 'interrupted')
        self.assertEqual(read_publication(self.journal)['watermark'], 1)

    def test_successful_request_with_wrong_token_is_not_trusted(self):
        self.power_loss_after_first_page()
        probe = self.data['probes']['request_ok_token_wrong']
        result = recover(self.journal, probe, new_snapshot=self.data['new_snapshot'],
                         full_pages=self.data['rescan_pages'])
        self.assertEqual(result['route'], 'rescan_new_snapshot')
        self.assertEqual(result['reason'], 'token_mismatch')
        self.assertEqual(result['watermark'], 2)

    def test_probe_cannot_extend_the_locally_recorded_expiry(self):
        self.power_loss_after_first_page()
        probe = deepcopy(self.data['probes']['token_expired'])
        probe['expires_at'] = '2026-05-02T00:00:00'  # widen the window locally
        result = recover(self.journal, probe, new_snapshot=self.data['new_snapshot'],
                         full_pages=self.data['rescan_pages'])
        self.assertEqual(result['route'], 'rescan_new_snapshot')
        self.assertEqual(result['reason'], 'token_expiry_widened')

    def test_old_pages_cannot_be_spliced_into_new_snapshot(self):
        self.power_loss_after_first_page()
        spliced = [deepcopy(self.data['pages'][0]), deepcopy(self.data['rescan_pages'][1])]
        with self.assertRaises(SnapshotChanged):
            recover(self.journal, self.data['probes']['token_expired'],
                    new_snapshot=self.data['new_snapshot'], full_pages=spliced)
        self.assertEqual(checkpoint(self.journal)['status'], 'interrupted')
        self.assertEqual(read_publication(self.journal)['watermark'], 1)

    def test_prefix_of_new_snapshot_is_not_a_full_rescan(self):
        self.power_loss_after_first_page()
        prefix = self.data['rescan_pages'][:1]
        with self.assertRaises(IncompleteScan):
            recover(self.journal, self.data['probes']['token_expired'],
                    new_snapshot=self.data['new_snapshot'], full_pages=prefix)
        self.assertEqual(checkpoint(self.journal)['snapshot'], 'E1')
        self.assertEqual(read_publication(self.journal)['watermark'], 1)

    def test_rescan_with_broken_cursor_chain_is_refused(self):
        self.power_loss_after_first_page()
        broken = deepcopy(self.data['rescan_pages'])
        broken[1]['cursor'] = 'p7'
        with self.assertRaises(PageCursorError):
            recover(self.journal, self.data['probes']['token_expired'],
                    new_snapshot=self.data['new_snapshot'], full_pages=broken)
        self.assertEqual(read_publication(self.journal)['watermark'], 1)

    def test_no_silent_splice_when_new_snapshot_uses_old_cursor_numbering(self):
        """Even with identical cursor names, snapshot identity is the boundary."""
        self.power_loss_after_first_page()
        stale = deepcopy(self.data['resume_pages'][0])  # E1 p2, saved too late
        result = recover(self.journal, self.data['probes']['token_expired'],
                         new_snapshot=self.data['new_snapshot'],
                         full_pages=self.data['rescan_pages'])
        self.assertEqual(result['route'], 'rescan_new_snapshot')
        with self.assertRaises(SnapshotChanged):
            accept_page(self.journal, stale)


class CrashBeforePublishTest(RecoveryBase):
    def test_power_loss_after_last_page_but_before_publish(self):
        """All pages were on disk; only the publish step was lost."""
        self.start()
        accept_page(self.journal, self.data['pages'][0])
        accept_page(self.journal, self.data['resume_pages'][0])
        self.assertEqual(checkpoint(self.journal)['status'], 'complete')
        result = recover(self.journal, self.data['probes']['token_valid'], resume_pages=[])
        self.assertEqual(result['route'], 'resume_snapshot')
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['coverage'], {'covered': 1, 'total': 2})
        self.assertEqual(read_publication(self.journal, 1)['snapshot'], 'E1')


class TwoWatermarksShareOneJournalTest(RecoveryBase):
    def test_same_power_loss_under_both_routes_yields_different_truths(self):
        resume = self._route(valid=True)
        rescan = self._route(valid=False)
        self.assertEqual(resume['coverage'], {'covered': 1, 'total': 2})
        self.assertEqual(rescan['coverage'], {'covered': 2, 'total': 2})
        self.assertEqual((resume['watermark'], rescan['watermark']), (1, 2))
        # Hard constraints hold for both.
        for result in (resume, rescan):
            published = result['publication']
            self.assertEqual({s['snapshot_id'] for s in published['scans']},
                             {published['snapshot']})
            devices = [s['device'] for s in published['scans']]
            self.assertEqual(len(devices), len(set(devices)))

    def _route(self, valid):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Path(tmp.name) / 'job.json'
        job = self.data['job']
        snapshot = job['snapshot']
        begin_job(journal, job_id=job['id'], required_build=self.data['required_build'],
                  snapshot=snapshot['id'], token=snapshot['continuation_token'],
                  expires_at=snapshot['expires_at'], first_cursor=snapshot['first_cursor'],
                  watermark=snapshot['watermark'], baseline=job['baseline'])
        accept_page(journal, self.data['pages'][0])
        probe = self.data['probes']['token_valid' if valid else 'token_expired']
        return recover(journal, probe, resume_pages=self.data['resume_pages'],
                       new_snapshot=self.data['new_snapshot'],
                       full_pages=self.data['rescan_pages'])


class InstallationsSnapshotTest(unittest.TestCase):
    def test_untagged_scan_lists_keep_historical_behaviour(self):
        data = fixture()
        self.assertEqual(coverage(data['old_complete'], 'v2'), {'covered': 1, 'total': 2})

    def test_publishable_latest_falls_back_to_published(self):
        data = fixture()
        devices = publishable_latest(data['pages'],
                                     published={'snapshot': 'E1', 'scans': data['old_complete']})
        self.assertEqual(sorted(devices), ['D1', 'D2'])
        self.assertEqual(devices['D2']['build'], 'v1')
        self.assertEqual({row['snapshot_id'] for row in devices.values()}, {'E1'})


if __name__ == '__main__':
    unittest.main()
