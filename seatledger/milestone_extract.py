"""Extract the effective on-site delivery milestone from offline OCR evidence.

Hard rules enforced here:

* Continuation rows inherit the header columns and item numbering assembled in
  ``contract_pages``; a later page or the repeated word "delivery" can never
  overwrite another milestone. Training / acceptance / delivery stay separate.
* An attachment or handwritten amendment changes the delivery date only when
  it is dual-signed for its own scope and explicitly names the replaced clause,
  and that clause resolves to a delivery row. An unresolvable target is held
  pending review — the maximum date is never picked automatically.
* Low-confidence dates, ambiguous date formats and missing pages quarantine
  the result as pending review with the raw OCR box attached; no deterministic
  engineer shift is emitted.
* Schedules already published to the project manager are immutable. A late
  clear scan produces a change record pointing at the old schedule revision,
  which stays readable with its original evidence references.
"""
from .contract_pages import pages, assemble_contracts, _is_legacy_event
from . import ocr_capture as ocr

DELIVERY = ocr.LABEL_DELIVERY
TRAINING = ocr.LABEL_TRAINING
ACCEPTANCE = ocr.LABEL_ACCEPTANCE
UNKNOWN = ocr.LABEL_UNKNOWN


def milestones(events):
    """Primary business result: engineer on-site date per contract.

    Legacy flat OCR rows keep the provisional semantics (last delivery row).
    Block evidence returns the effective delivery ISO date, or ``None`` while
    the evidence is quarantined pending review.
    """
    events = list(events)
    if events and all(_is_legacy_event(event) for event in events):
        result = {}
        for contract, rows in pages(events).items():
            dates = [row['date'] for row in rows if row['label'] == 'delivery']
            result[contract] = dates[-1] if dates else None
        return result
    return {
        contract: extracted['delivery']['date']
        for contract, extracted in extract_contracts(events).items()
    }


def extract_contracts(blocks):
    """Resolve every contract into an evidenced delivery decision."""
    assembled = assemble_contracts(blocks)
    return {
        contract: _extract_one(doc)
        for contract, doc in assembled.items()
    }


def extract_contract(blocks, contract_id):
    return extract_contracts(blocks)[contract_id]


def _extract_one(doc):
    issues = list(doc['issues'])
    milestones = []
    rows_by_item = {}
    for table in doc['tables'].values():
        for row in table['rows']:
            rows_by_item.setdefault(row['item_no'], []).append(row)
            milestones.append(_milestone_view(row))

    amendments = []
    delivery_row = _select_delivery_row(rows_by_item)
    delivery = _delivery_from_row(doc, delivery_row, issues)

    for amendment in doc['amendments']:
        verdict = _resolve_amendment(doc, amendment, rows_by_item, issues)
        amendments.append(verdict)
        if verdict['status'] == 'applied_delivery':
            target = verdict['target_row']
            delivery = {
                'date': amendment['date']['date'],
                'status': 'confirmed',
                'source': 'amendment',
                'evidence': _delivery_evidence(doc, target, amendment, verdict),
                'supersedes_date': target['date']['date'],
            }
        elif verdict['status'] == 'applied_non_delivery':
            for item in milestones:
                if item['item_no'] == amendment['target_clause']:
                    item['effective_date'] = amendment['date']['date']
                    item['amended_by'] = amendment['amendment_id']

    pending_codes = {issue['code'] for issue in issues}
    if bool(doc['missing_pages']) and delivery['date'] is not None:
        # 缺页意味着签署覆盖范围无法闭合：候选日期保留在证据中，
        # 决策层置空，不得自动排出工程师班次。
        issues.append({
            'code': 'delivery_quarantined_missing_page',
            'candidate_date': delivery['date'],
            'missing_pages': doc['missing_pages'],
            'message': '存在缺失扫描页，现场交付候选日期隔离待核',
        })
        delivery = {
            'date': None,
            'status': 'pending_review',
            'source': delivery['source'],
            'evidence': delivery['evidence'],
            'candidate_date': delivery['date'],
            'blocked_by': 'page_missing',
        }
    if delivery['date'] is None:
        status = 'pending_review'
    else:
        status = 'confirmed'
    open_pending = [
        verdict for verdict in amendments
        if verdict['status'].startswith('pending') or verdict['status'].startswith('blocked')
    ]
    return {
        'contract_id': doc['contract_id'],
        'status': status,
        'delivery': delivery,
        'milestones': milestones,
        'amendments': amendments,
        'signatures': _signature_summary(doc),
        'missing_pages': doc['missing_pages'],
        'issues': issues,
        'open_pending': [verdict['amendment_id'] for verdict in open_pending],
        'pending_codes': sorted(pending_codes),
    }


def _select_delivery_row(rows_by_item):
    """Pick the operative delivery row by clause scope, never page order.

    Multiple delivery rows are legal (a rescan may reveal a later numbered
    delivery item). The highest item number carrying a usable date is the
    operative on-site delivery; earlier delivery rows remain in the evidence.
    """
    delivery_rows = [
        row for rows in rows_by_item.values() for row in rows
        if row['label'] == DELIVERY
    ]
    if not delivery_rows:
        return None
    delivery_rows.sort(key=lambda row: (int(row['item_no']) if row['item_no'].isdigit() else -1,
                                        row['page']))
    return delivery_rows[-1]


def _delivery_from_row(doc, row, issues):
    if row is None:
        issues.append({
            'code': 'delivery_row_missing',
            'message': '未找到可确认的现场交付行，培训与验收行不得充当交付',
        })
        return {'date': None, 'status': 'pending_review', 'source': None, 'evidence': None}
    if row['date']['status'] != 'ok':
        return {
            'date': None,
            'status': 'pending_review',
            'source': 'table',
            'evidence': _row_evidence(doc, row),
        }
    return {
        'date': row['date']['date'],
        'status': 'confirmed',
        'source': 'table',
        'evidence': _row_evidence(doc, row),
    }


def _milestone_view(row):
    return {
        'item_no': row['item_no'],
        'kind': row['label'],
        'label_raw': row['label_raw'],
        'date': row['date']['date'],
        'date_status': row['date']['status'],
        'effective_date': row['date']['date'],
        'page': row['page'],
        'table_id': row['table_id'],
        'header_page': row['header_page'],
        'continuation': row['continuation'],
        'amended_by': None,
    }


def _resolve_amendment(doc, amendment, rows_by_item, issues):
    """Decide whether a signed amendment may move the delivery date."""
    verdict = {
        'amendment_id': amendment['amendment_id'],
        'page': amendment['page'],
        'target_clause': amendment['target_clause'],
        'date': amendment['date']['date'],
        'status': None,
        'reason': None,
        'target_row': None,
        'signatures': _scope_signatures(doc, amendment['signature_scope']),
        'raw_text': amendment['raw_text'],
        'bbox': amendment['bbox'],
    }
    dual_signed = any(box['dual_signed'] for box in verdict['signatures'])
    if not dual_signed:
        verdict['status'] = 'rejected_unsigned'
        verdict['reason'] = '修订页缺少双方有效签署，不改变任何日期'
        return verdict
    if not amendment['replaces_clause'] or not amendment['target_clause']:
        verdict['status'] = 'rejected_unscoped'
        verdict['reason'] = '修订未明确指向被替换条款'
        return verdict
    targets = rows_by_item.get(amendment['replaces_clause'])
    if not targets:
        verdict['status'] = 'rejected_unscoped'
        verdict['reason'] = f"被替换条款第 {amendment['replaces_clause']} 项在表中不存在"
        return verdict
    target = targets[-1]
    verdict['target_row'] = target
    if amendment['date']['status'] != 'ok':
        verdict['status'] = 'pending_date_review'
        verdict['reason'] = '修订日期低置信或格式歧义，保留原文框待核'
        issues.append({
            'code': 'amendment_date_pending',
            'amendment_id': amendment['amendment_id'],
            'raw_text': amendment['raw_text'],
            'bbox': amendment['bbox'],
        })
        return verdict
    if target['label'] == UNKNOWN:
        verdict['status'] = 'pending_target_resolution'
        verdict['reason'] = (
            f"第 {amendment['replaces_clause']} 项里程碑名称在 OCR 中丢失，"
            '须以清晰重扫的原表目录核对该行后才能判断是否改变现场交付'
        )
        issues.append({
            'code': 'amendment_target_unknown',
            'amendment_id': amendment['amendment_id'],
            'page': amendment['page'],
            'target_clause': amendment['target_clause'],
            'raw_text': amendment['raw_text'],
            'bbox': amendment['bbox'],
            'target_bbox': target['label_bbox'],
            'target_raw_text': target['label_raw'],
        })
        return verdict
    if doc['missing_pages']:
        verdict['status'] = 'blocked_missing_page'
        verdict['reason'] = '存在缺失扫描页，签署覆盖范围无法闭合，修订暂不生效'
        issues.append({
            'code': 'amendment_blocked_missing_page',
            'amendment_id': amendment['amendment_id'],
            'missing_pages': doc['missing_pages'],
        })
        return verdict
    if target['label'] == DELIVERY:
        verdict['status'] = 'applied_delivery'
        verdict['reason'] = '双方签署且明确替换现场交付条款，现场交付日期改按修订执行'
    else:
        verdict['status'] = 'applied_non_delivery'
        verdict['reason'] = f"修订指向{target['label']}条款，现场交付日期不变"
    return verdict


def _scope_signatures(doc, scope):
    return [
        {
            'signature_id': box['signature_id'],
            'page': box['page'],
            'scope': box['scope'],
            'dual_signed': box['dual_signed'],
            'parties': [
                {'party': party['party'], 'party_id': party['party_id'],
                 'signed': party['signed'], 'conf': party['conf']}
                for party in box['parties']
            ],
        }
        for box in doc['signatures'] if box['scope'] == scope
    ]


def _row_evidence(doc, row):
    return {
        'kind': 'table_row',
        'contract_id': doc['contract_id'],
        'page': row['page'],
        'table_id': row['table_id'],
        'item_no': row['item_no'],
        'clause': f"第 {row['item_no']} 项",
        'header_page': row['header_page'],
        'continuation': row['continuation'],
        'label_raw': row['label_raw'],
        'label_bbox': row['label_bbox'],
        'date_raw': row['date']['raw_text'],
        'date_bbox': row['date']['bbox'],
        'date_rule': row['date']['rule'],
        'signatures': _scope_signatures(doc, row['signature_scope']),
    }


def _delivery_evidence(doc, row, amendment, verdict):
    evidence = _row_evidence(doc, row)
    evidence['amendment'] = {
        'kind': 'amendment',
        'amendment_id': amendment['amendment_id'],
        'page': amendment['page'],
        'block_id': amendment['block_id'],
        'raw_text': amendment['raw_text'],
        'bbox': amendment['bbox'],
        'target_clause': amendment['target_clause'],
        'replaces_clause': amendment['replaces_clause'],
        'date_raw': amendment['date']['raw_text'],
        'date_rule': amendment['date']['rule'],
        'signatures': verdict['signatures'],
    }
    return evidence


def _signature_summary(doc):
    return [
        {
            'signature_id': box['signature_id'],
            'page': box['page'],
            'scope': box['scope'],
            'dual_signed': box['dual_signed'],
        }
        for box in doc['signatures']
    ]


class ScheduleLedger:
    """Append-only engineer schedule ledger for project managers.

    Confirmed schedules receive immutable numbered revisions. Pending evidence
    is quarantined without a deterministic shift and never overwrites a
    published schedule. A later clear scan that changes the date opens a new
    revision carrying an explicit back-reference to the superseded one.
    """

    def __init__(self):
        self._revisions = {}
        self._pending = {}

    def publish(self, extraction, published_at, reference=None):
        contract = extraction['contract_id']
        if extraction['status'] != 'confirmed' or not extraction['delivery']['date']:
            record = {
                'contract_id': contract,
                'status': 'pending_review',
                'delivery_date': None,
                'published_at': published_at,
                'reference': reference,
                'issues': extraction['issues'],
                'quarantined': True,
            }
            self._pending[contract] = record
            return record
        revision = len(self._revisions.setdefault(contract, [])) + 1
        record = {
            'contract_id': contract,
            'schedule_id': f'{contract}:S{revision}',
            'revision': revision,
            'status': 'published',
            'delivery_date': extraction['delivery']['date'],
            'published_at': published_at,
            'reference': reference,
            'supersedes': None,
            'change_reason': None,
            'evidence': extraction['delivery']['evidence'],
        }
        self._revisions[contract].append(record)
        return record

    def revise(self, extraction, changed_at, reason, change_refs=None):
        """Apply a late clear scan. Silent overwrites are impossible: a changed
        date opens a numbered revision referencing the old schedule."""
        contract = extraction['contract_id']
        current = self.read(contract)
        if extraction['status'] != 'confirmed' or not extraction['delivery']['date']:
            record = {
                'contract_id': contract,
                'status': 'pending_review',
                'delivery_date': None,
                'changed_at': changed_at,
                'issues': extraction['issues'],
                'quarantined': True,
                'kept_schedule': current['schedule_id'] if current else None,
            }
            self._pending[contract] = record
            return record
        if current and current['delivery_date'] == extraction['delivery']['date']:
            return {
                'contract_id': contract,
                'status': 'unchanged',
                'schedule_id': current['schedule_id'],
                'delivery_date': current['delivery_date'],
                'reason': reason,
                'change_refs': change_refs or [],
            }
        revision = len(self._revisions.setdefault(contract, [])) + 1
        record = {
            'contract_id': contract,
            'schedule_id': f'{contract}:S{revision}',
            'revision': revision,
            'status': 'changed',
            'delivery_date': extraction['delivery']['date'],
            'published_at': changed_at,
            'supersedes': current['schedule_id'] if current else None,
            'previous_delivery_date': current['delivery_date'] if current else None,
            'change_reason': reason,
            'change_refs': change_refs or [],
            'evidence': extraction['delivery']['evidence'],
        }
        self._revisions[contract].append(record)
        return record

    def read(self, contract):
        revisions = self._revisions.get(contract)
        return revisions[-1] if revisions else None

    def pending(self, contract):
        return self._pending.get(contract)

    def history(self, contract):
        return list(self._revisions.get(contract, []))
