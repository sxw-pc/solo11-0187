"""Collect offline OCR page blocks by contract and assemble cross-page tables.

The legacy flat-event contract (``contract_id/page/label/date`` rows, as in
fixtures/contract-ocr-ok.json) keeps its original aggregation semantics.
Structured OCR blocks (coordinates, rows/columns, signature boxes) are
assembled page by page: continuation tables inherit the header columns and
item numbering of the table that started on an earlier page instead of
re-deriving meaning from a repeated word or a later page number.
"""
from . import ocr_capture as ocr


def _is_legacy_event(event):
    return (
        isinstance(event, dict)
        and 'kind' not in event
        and 'block_id' not in event
        and 'label' in event
        and 'date' in event
    )


def pages(events):
    """Group OCR evidence by contract.

    Legacy flat events return the original per-contract event lists. Block
    evidence returns assembled page structures carrying the resolved table
    rows, so continuation rows expose the header they inherited.
    """
    events = list(events)
    if events and all(_is_legacy_event(event) for event in events):
        result = {}
        for event in events:
            result.setdefault(event['contract_id'], []).append(event)
        return result
    assembled = assemble_contracts(events)
    return {contract: doc['pages'] for contract, doc in assembled.items()}


def assemble_contracts(blocks):
    """Build the full per-contract structural view from raw OCR blocks."""
    result = {}
    for contract, contract_blocks in ocr.group_by_contract(blocks).items():
        year = ocr.infer_contract_year(contract_blocks)
        page_numbers, missing_pages = ocr.detect_missing_pages(contract_blocks)
        doc = {
            'contract_id': contract,
            'year': year,
            'page_numbers': page_numbers,
            'missing_pages': missing_pages,
            'pages': [],
            'tables': {},
            'amendments': [],
            'signatures': [],
            'issues': [],
        }
        for page in page_numbers:
            doc['pages'].append({'page': page, 'blocks': [], 'table_rows': []})
        if missing_pages:
            doc['issues'].append({
                'code': 'page_missing',
                'pages': missing_pages,
                'message': '扫描页编号不连续，缺失页可能承载决定交付日期的表格行或签署框',
            })
        _assemble_tables(doc, contract_blocks, year)
        _assemble_amendments(doc, contract_blocks, year)
        _assemble_signatures(doc, contract_blocks)
        result[contract] = doc
    return result


def _page_slot(doc, page):
    for slot in doc['pages']:
        if slot['page'] == page:
            return slot
    slot = {'page': page, 'blocks': [], 'table_rows': []}
    doc['pages'].append(slot)
    doc['pages'].sort(key=lambda item: item['page'])
    return slot


def _assemble_tables(doc, blocks, year):
    ordered = [b for b in blocks if b.get('kind') == 'table']
    ordered.sort(key=lambda block: (block.get('page', 0), block.get('block_id', '')))
    table_columns = {}
    for block in ordered:
        table_id = block['table_id']
        page = block.get('page')
        slot = _page_slot(doc, page)
        slot['blocks'].append({
            'block_id': block['block_id'],
            'kind': 'table',
            'table_id': table_id,
            'continuation': bool(block.get('continuation')),
            'header_page': block.get('header_carried_from_page'),
        })
        table = doc['tables'].setdefault(table_id, {
            'table_id': table_id,
            'header_page': None,
            'columns': [],
            'rows': [],
        })
        if block.get('header'):
            columns = ocr.cluster_columns(block['header'])
            table_columns[table_id] = columns
            table['columns'] = columns
            table['header_page'] = page
        elif block.get('continuation') and table_id in table_columns:
            columns = table_columns[table_id]
        else:
            columns = table_columns.get(table_id, [])
        header_page = block.get('header_carried_from_page') or table['header_page']
        for row_index, raw_row in enumerate(block.get('rows', [])):
            row = _assemble_row(block, raw_row, row_index, columns, header_page, year)
            table['rows'].append(row)
            slot['table_rows'].append(row)
            if row['label'] == ocr.LABEL_UNKNOWN:
                doc['issues'].append({
                    'code': 'label_illegible_or_unknown',
                    'page': page,
                    'table_id': table_id,
                    'item_no': row['item_no'],
                    'raw_text': row['label_raw'],
                    'bbox': row['label_bbox'],
                    'message': '里程碑名称低置信或不可辨认，不得按同词 delivery 推断',
                })
            if row['date']['status'] != 'ok':
                doc['issues'].append({
                    'code': 'date_pending_review',
                    'page': page,
                    'table_id': table_id,
                    'item_no': row['item_no'],
                    'raw_text': row['date']['raw_text'],
                    'bbox': row['date']['bbox'],
                    'rule': row['date']['rule'],
                    'message': '日期低置信或格式歧义，保留原文框待人工核对',
                })


def _assemble_row(block, raw_row, row_index, columns, header_page, year):
    cells_by_role = {'item': [], 'label': [], 'date': [], 'ignore': []}
    for cell in raw_row:
        column = ocr.match_column(columns, cell['bbox']) if columns else {'role': 'ignore'}
        cells_by_role.setdefault(column['role'], []).append(cell)

    def first(role):
        bucket = cells_by_role.get(role) or []
        return bucket[0] if bucket else {}

    item_cell = first('item')
    label_cell = first('label')
    date_cell = first('date')
    note_cell = first('ignore')
    label_conf = float(label_cell.get('conf', 0.0))
    label_raw = str(label_cell.get('text', '')).strip()
    return {
        'table_id': block['table_id'],
        'block_id': block['block_id'],
        'page': block.get('page'),
        'row_index': row_index,
        'continuation': bool(block.get('continuation')),
        'header_page': header_page,
        'item_no': str(item_cell.get('text', '')).strip(),
        'label': ocr.classify_label(label_raw, label_conf),
        'label_raw': label_raw,
        'label_conf': label_conf,
        'label_illegible': bool(label_cell.get('illegible', False)) or label_conf < ocr.CELL_CONF_FLOOR,
        'label_bbox': label_cell.get('bbox'),
        'date': ocr.read_date_cell(date_cell, year),
        'note': str(note_cell.get('text', '')).strip(),
        'signature_scope': f"table:{block['table_id']}",
    }


def _assemble_amendments(doc, blocks, year):
    for block in blocks:
        if block.get('kind') != 'amendment':
            continue
        page = block.get('page')
        slot = _page_slot(doc, page)
        slot['blocks'].append({
            'block_id': block['block_id'],
            'kind': 'amendment',
            'amendment_id': block.get('amendment_id'),
        })
        date_info = ocr.read_date_cell(
            {'text': block.get('date_text', ''), 'bbox': block.get('bbox'),
             'conf': block.get('conf', 0.0)},
            year,
        )
        amendment = {
            'amendment_id': block.get('amendment_id'),
            'block_id': block['block_id'],
            'page': page,
            'handwritten': bool(block.get('handwritten')),
            'conf': float(block.get('conf', 0.0)),
            'raw_text': block.get('raw_text', ''),
            'bbox': block.get('bbox'),
            'target_clause': str(block.get('target_clause', '')).strip(),
            'replaces_clause': str(block.get('replaces_clause', '')).strip(),
            'date': date_info,
            'signature_scope': f"amendment:{block.get('amendment_id')}",
        }
        doc['amendments'].append(amendment)
        if amendment['conf'] < ocr.CELL_CONF_FLOOR or date_info['status'] != 'ok':
            doc['issues'].append({
                'code': 'amendment_pending_review',
                'page': page,
                'amendment_id': amendment['amendment_id'],
                'raw_text': amendment['raw_text'],
                'bbox': amendment['bbox'],
                'message': '手写修订 OCR 低置信或日期不可标准化，待清晰重扫',
            })


def _assemble_signatures(doc, blocks):
    for block in blocks:
        if block.get('kind') != 'signature':
            continue
        evidence = ocr.signature_evidence(block)
        doc['signatures'].append(evidence)
        slot = _page_slot(doc, block.get('page'))
        slot['blocks'].append({
            'block_id': block['block_id'],
            'kind': 'signature',
            'signature_id': evidence['signature_id'],
            'scope': evidence['scope'],
            'dual_signed': evidence['dual_signed'],
        })
        if not evidence['dual_signed']:
            doc['issues'].append({
                'code': 'signature_incomplete',
                'page': block.get('page'),
                'scope': evidence['scope'],
                'bbox': evidence['bbox'],
                'message': '签署框未达到双方有效签署，相关页内容不能单独改变交付日期',
            })
