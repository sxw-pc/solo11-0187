import copy
import json
import unittest
from pathlib import Path

from seatledger import ocr_capture as ocr
from seatledger.contract_pages import pages, assemble_contracts
from seatledger.milestone_extract import (
    milestones, extract_contract, extract_contracts, ScheduleLedger,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))


class ContractMilestoneTest(unittest.TestCase):
    def setUp(self):
        self.doc = fixture('contract-table.json')
        self.blocks = self.doc['blocks']

    def _c1_assembled(self, blocks=None):
        return assemble_contracts(blocks or self.blocks)['C1']

    # 1. 跨页表头与项目编号继承 ----------------------------------------------
    def test_continuation_row_inherits_header_and_item_number(self):
        doc = self._c1_assembled()
        table = doc['tables']['C1-T1']
        # 表头只在第 1 页，续页行必须继承第 1 页列标题
        self.assertEqual(table['header_page'], 1)
        continuation = [row for row in table['rows'] if row['continuation']]
        self.assertEqual({row['header_page'] for row in continuation}, {1})
        item_nos = [row['item_no'] for row in table['rows']]
        self.assertEqual(item_nos, ['1', '2', '4'])
        # pages() 同样按合同聚合，并暴露续页继承的表头
        view = pages(self.blocks)['C1']
        page2 = next(slot for slot in view if slot['page'] == 2)
        row = next(item for item in page2['table_rows'] if item['item_no'] == '2')
        self.assertEqual(row['header_page'], 1)
        self.assertEqual(row['label'], ocr.LABEL_TRAINING)

    # 2. 培训/验收行不得覆盖现场交付 ------------------------------------------
    def test_training_continuation_does_not_overwrite_delivery(self):
        result = milestones(self.blocks)
        # 旧实现取最后一条 delivery 得到 2026-12-10；正确结果是 12-01
        self.assertEqual(result['C1'], '2026-12-01')
        extracted = extract_contract(self.blocks, 'C1')
        self.assertEqual(extracted['delivery']['date'], '2026-12-01')
        kinds = {m['item_no']: m['kind'] for m in extracted['milestones']}
        self.assertEqual(kinds['1'], ocr.LABEL_DELIVERY)
        self.assertEqual(kinds['2'], ocr.LABEL_TRAINING)
        self.assertEqual(kinds['4'], ocr.LABEL_UNKNOWN)
        # 任何里程碑都不允许仅凭同词 delivery 或页码靠后生效
        self.assertNotIn('2026-12-10', [m['effective_date'] for m in extracted['milestones']
                                       if m['kind'] == ocr.LABEL_DELIVERY])

    def test_evidence_carries_page_table_clause_and_signature(self):
        extracted = extract_contract(self.blocks, 'C1')
        evidence = extracted['delivery']['evidence']
        self.assertEqual(evidence['page'], 1)
        self.assertEqual(evidence['table_id'], 'C1-T1')
        self.assertEqual(evidence['clause'], '第 1 项')
        self.assertEqual(evidence['date_raw'], '2026-12-01')
        self.assertTrue(evidence['signatures'])
        self.assertTrue(evidence['signatures'][0]['dual_signed'])
        parties = {p['party_id'] for p in evidence['signatures'][0]['parties']}
        self.assertEqual(parties, {'P-A', 'P-B'})

    # 3. 手写修订的指向与签署范围 ---------------------------------------------
    def test_handwritten_amendment_with_lost_target_is_held_pending(self):
        extracted = extract_contract(self.blocks, 'C1')
        amendment = next(a for a in extracted['amendments']
                         if a['amendment_id'] == 'C1-A1')
        self.assertEqual(amendment['status'], 'pending_target_resolution')
        self.assertIn('amendment_target_unknown', extracted['pending_codes'])
        # 待核状态下交付仍为 12-01，绝不自动取最大日期 12-15
        self.assertEqual(extracted['delivery']['date'], '2026-12-01')
        self.assertEqual(extracted['status'], 'confirmed')
        self.assertEqual(extracted['open_pending'], ['C1-A1'])
        issue = next(i for i in extracted['issues']
                     if i['code'] == 'amendment_target_unknown')
        self.assertEqual(issue['target_bbox'], [175, 230, 210, 260])
        self.assertEqual(issue['raw_text'],
                         '手写补充：经双方同意，原表第4项延期至12-15，其余条款不变。')

    def test_unsigned_amendment_cannot_change_delivery(self):
        blocks = copy.deepcopy(self.blocks)
        sign = next(b for b in blocks
                    if b.get('signature_id') == 'C1-S3')
        sign['signed'][1]['signed'] = False
        extracted = extract_contract(blocks, 'C1')
        amendment = next(a for a in extracted['amendments']
                         if a['amendment_id'] == 'C1-A1')
        self.assertEqual(amendment['status'], 'rejected_unsigned')
        self.assertEqual(extracted['delivery']['date'], '2026-12-01')

    def test_amendment_without_named_clause_is_rejected(self):
        blocks = copy.deepcopy(self.blocks)
        amend = next(b for b in blocks
                     if b.get('amendment_id') == 'C1-A1')
        amend['replaces_clause'] = ''
        extracted = extract_contract(blocks, 'C1')
        amendment = extracted['amendments'][0]
        self.assertEqual(amendment['status'], 'rejected_unscoped')
        self.assertEqual(extracted['delivery']['date'], '2026-12-01')

    # 4. 低置信、格式歧义与缺页 ------------------------------------------------
    def test_low_confidence_date_is_quarantined_with_raw_box(self):
        extracted = extract_contract(self.blocks, 'C2')
        self.assertEqual(extracted['status'], 'pending_review')
        self.assertIsNone(extracted['delivery']['date'])
        issue = next(i for i in extracted['issues']
                     if i['code'] == 'date_pending_review')
        self.assertEqual(issue['raw_text'], '2026-12-03')
        self.assertEqual(issue['bbox'], [335, 190, 420, 220])
        ledger = ScheduleLedger()
        record = ledger.publish(extracted, '2026-11-20T12:00')
        self.assertTrue(record['quarantined'])
        self.assertIsNone(ledger.read('C2'))  # 不得排出确定班次

    def test_ambiguous_slash_date_is_held_pending(self):
        extracted = extract_contract(self.blocks, 'C3')
        self.assertEqual(extracted['status'], 'pending_review')
        self.assertIsNone(extracted['delivery']['date'])
        issue = next(i for i in extracted['issues']
                     if i['code'] == 'date_pending_review')
        self.assertEqual(issue['rule'], 'slash_ambiguous')

    def test_missing_page_blocks_signed_amendment(self):
        extracted = extract_contract(self.blocks, 'C4')
        # 第 2 页缺失，即使修订双方签署且指向第 1 项也暂不生效
        self.assertEqual(extracted['missing_pages'], [2])
        amendment = extracted['amendments'][0]
        self.assertEqual(amendment['status'], 'blocked_missing_page')
        self.assertEqual(extracted['status'], 'pending_review')
        self.assertIsNone(extracted['delivery']['date'])

    # 5. 两路清晰重扫 ---------------------------------------------------------
    def _rescan_blocks(self, path):
        rescan = self.doc['rescans'][path]
        merged, replacements = ocr.merge_rescans(self.blocks, rescan['blocks'])
        return merged, replacements, rescan

    def test_rescan_proving_delivery_moves_engineers_to_12_15(self):
        merged, replacements, rescan = self._rescan_blocks('delivery')
        self.assertEqual({r['rescan_of'] for r in replacements},
                         {'C1-p2-table', 'C1-p3-amend'})
        extracted = extract_contract(merged, 'C1')
        self.assertEqual(extracted['delivery']['date'], '2026-12-15')
        self.assertEqual(extracted['delivery']['source'], 'amendment')
        self.assertEqual(extracted['delivery']['supersedes_date'], '2026-12-12')
        amendment = next(a for a in extracted['amendments']
                         if a['amendment_id'] == 'C1-A1')
        self.assertEqual(amendment['status'], 'applied_delivery')
        # 被替换的模糊 OCR 框仍随重扫块保留可回查
        fresh = next(b for b in merged if b['block_id'] == 'C1-p2-table')
        self.assertEqual(fresh['superseded_block']['block_id'], 'C1-p2-table')
        self.assertTrue(extracted['delivery']['evidence']['amendment'])

    def test_rescan_proving_training_keeps_delivery_at_12_01(self):
        merged, _replacements, _rescan = self._rescan_blocks('training')
        extracted = extract_contract(merged, 'C1')
        self.assertEqual(extracted['delivery']['date'], '2026-12-01')
        amendment = next(a for a in extracted['amendments']
                         if a['amendment_id'] == 'C1-A1')
        self.assertEqual(amendment['status'], 'applied_non_delivery')
        training = [m for m in extracted['milestones']
                    if m['item_no'] == '4' and m['kind'] == ocr.LABEL_TRAINING]
        self.assertEqual(len(training), 1)
        self.assertEqual(training[0]['effective_date'], '2026-12-15')
        self.assertEqual(training[0]['amended_by'], 'C1-A1')
        # 12-10 培训单列，不与交付混淆
        training_10 = next(m for m in extracted['milestones']
                           if m['item_no'] == '2')
        self.assertEqual(training_10['date'], '2026-12-10')
        self.assertEqual(training_10['kind'], ocr.LABEL_TRAINING)

    # 6. 已发布日程不可静默改写，旧日程留痕回读 -------------------------------
    def test_published_schedule_keeps_revision_chain_on_late_rescan(self):
        ledger = ScheduleLedger()
        base = extract_contract(self.blocks, 'C1')
        first = ledger.publish(base, '2026-11-20T12:00', reference='PM-PLAN-001')
        self.assertEqual(first['schedule_id'], 'C1:S1')
        self.assertEqual(first['delivery_date'], '2026-12-01')

        merged, replacements, rescan = self._rescan_blocks('delivery')
        rescanned = extract_contract(merged, 'C1')
        change = ledger.revise(
            rescanned, rescan['captured_at'],
            reason='迟到清晰重扫证明第4项为现场交付，修订双方签署生效',
            change_refs=[r['block_id'] for r in replacements],
        )
        self.assertEqual(change['status'], 'changed')
        self.assertEqual(change['schedule_id'], 'C1:S2')
        self.assertEqual(change['delivery_date'], '2026-12-15')
        # 变更必须指向旧日程并保留原引用
        self.assertEqual(change['supersedes'], 'C1:S1')
        self.assertEqual(change['previous_delivery_date'], '2026-12-01')

        current = ledger.read('C1')
        self.assertEqual(current['delivery_date'], '2026-12-15')
        history = ledger.history('C1')
        self.assertEqual([row['revision'] for row in history], [1, 2])
        old = history[0]
        self.assertEqual(old['delivery_date'], '2026-12-01')
        self.assertEqual(old['reference'], 'PM-PLAN-001')
        self.assertTrue(old['evidence'])
        self.assertIn('C1-p2-table', change['change_refs'])

    def test_training_rescan_leaves_published_schedule_untouched(self):
        ledger = ScheduleLedger()
        ledger.publish(extract_contract(self.blocks, 'C1'),
                       '2026-11-20T12:00', reference='PM-PLAN-001')
        merged, _replacements, rescan = self._rescan_blocks('training')
        change = ledger.revise(
            extract_contract(merged, 'C1'), rescan['captured_at'],
            reason='重扫证明第4项仅为培训',
        )
        self.assertEqual(change['status'], 'unchanged')
        self.assertEqual(change['schedule_id'], 'C1:S1')
        self.assertEqual(ledger.read('C1')['delivery_date'], '2026-12-01')
        self.assertEqual(len(ledger.history('C1')), 1)

    def test_pending_rescan_does_not_overwrite_published_schedule(self):
        ledger = ScheduleLedger()
        ledger.publish(extract_contract(self.blocks, 'C1'),
                       '2026-11-20T12:00')
        # 重扫后修订页与第 4 项交付日期均仍为低置信：证据待核，
        # 旧日程不得被静默改写
        merged, _replacements, rescan = self._rescan_blocks('delivery')
        for block in merged:
            if block.get('amendment_id') == 'C1-A1':
                block['conf'] = 0.4
        table = next(b for b in merged if b['block_id'] == 'C1-p2-table')
        for row in table['rows']:
            if row[0]['text'] == '4':
                row[2]['conf'] = 0.4
        record = ledger.revise(extract_contract(merged, 'C1'),
                               rescan['captured_at'], reason='重扫交付证据仍不清晰')
        self.assertTrue(record['quarantined'])
        self.assertEqual(record['kept_schedule'], 'C1:S1')
        self.assertEqual(ledger.read('C1')['delivery_date'], '2026-12-01')

    # 7. 旧扁平夹具与既有种子测试语义保持 --------------------------------------
    def test_legacy_flat_fixture_keeps_provisional_semantics(self):
        seed = fixture('contract-ocr-ok.json')
        self.assertEqual(milestones(seed['pages']), {'C1': '2026-12-10'})
        grouped = pages(seed['pages'])
        self.assertEqual([row['page'] for row in grouped['C1']], [1, 2])

    def test_all_contracts_extracted_without_side_effects(self):
        result = extract_contracts(self.blocks)
        self.assertEqual(set(result), {'C1', 'C2', 'C3', 'C4'})
        # 主营结果只回答工程师到场日期
        self.assertEqual(milestones(self.blocks)['C1'], '2026-12-01')
        self.assertIsNone(milestones(self.blocks)['C2'])


if __name__ == '__main__':
    unittest.main()
