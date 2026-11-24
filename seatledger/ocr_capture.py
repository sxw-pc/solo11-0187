"""Offline OCR capture layer.

Turns raw OCR blocks (bounding boxes, table cells, signature boxes, handwritten
amendment boxes) into normalized evidence fragments. Nothing here decides the
business milestone: low confidence, ambiguous date formats and missing pages
are flagged instead of guessed, and every normalized value keeps its raw box
so a project manager can trace it back to the scanned page.
"""
import copy
import re

# OCR confidence gates. At or below these thresholds evidence is never allowed
# to produce a deterministic engineer shift on its own.
DATE_CONF_FLOOR = 0.60
SIGN_CONF_FLOOR = 0.60
CELL_CONF_FLOOR = 0.60

LABEL_DELIVERY = 'delivery'
LABEL_TRAINING = 'training'
LABEL_ACCEPTANCE = 'acceptance'
LABEL_UNKNOWN = 'unknown'

_DELIVERY_WORDS = ('现场交付', '到场交付', '交付')
_TRAINING_WORDS = ('培训',)
_ACCEPTANCE_WORDS = ('验收',)

_ISO_RE = re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$')
_SHORT_RE = re.compile(r'^(\d{1,2})[-/.](\d{1,2})$')
_SLASH_RE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')


def group_by_contract(blocks):
    """Group raw OCR blocks by contract_id, preserving capture order."""
    grouped = {}
    for block in blocks:
        grouped.setdefault(block['contract_id'], []).append(block)
    return grouped


def infer_contract_year(blocks):
    """Infer the working year from unambiguous ISO dates and meta blocks."""
    for block in blocks:
        if block.get('kind') == 'meta' and block.get('year_hint'):
            return int(block['year_hint'])
    for block in blocks:
        if block.get('kind') != 'table':
            continue
        for row in block.get('rows', []):
            for cell in row:
                text = str(cell.get('text', '')).strip()
                match = _ISO_RE.match(text)
                if match and float(cell.get('conf', 1.0)) >= DATE_CONF_FLOOR:
                    return int(match.group(1))
    return None


def normalize_date(text, conf, contract_year):
    """Normalize an OCR date cell.

    Returns (iso_date_or_None, rule, ambiguous). Ambiguous formats (e.g.
    MM/DD/YYYY vs DD/MM/YYYY) are returned with ambiguous=True so callers keep
    them pending review instead of scheduling engineers on a guess.
    """
    raw = str(text).strip()
    conf = float(conf)
    iso = _ISO_RE.match(raw)
    if iso:
        year, month, day = (int(iso.group(i)) for i in (1, 2, 3))
        return _build_date(year, month, day), 'iso', False
    slash = _SLASH_RE.match(raw)
    if slash:
        first, second, year = (int(slash.group(i)) for i in (1, 2, 3))
        if first > 12 and second <= 12:
            return _build_date(year, second, first), 'slash_ddmm', False
        if second > 12 and first <= 12:
            return _build_date(year, first, second), 'slash_mmdd', False
        # Both interpretations are legal calendars: cannot disambiguate.
        return None, 'slash_ambiguous', True
    short = _SHORT_RE.match(raw)
    if short:
        if not contract_year:
            return None, 'short_missing_year', True
        month, day = int(short.group(1)), int(short.group(2))
        return _build_date(contract_year, month, day), 'short_with_year_hint', False
    return None, 'unparsed', True


def _build_date(year, month, day):
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f'{year:04d}-{month:02d}-{day:02d}'


def read_date_cell(cell, contract_year):
    """Normalize one date cell, attaching the raw box and confidence."""
    raw = str(cell.get('text', '')).strip()
    conf = float(cell.get('conf', 0.0))
    iso, rule, ambiguous = normalize_date(raw, conf, contract_year)
    result = {
        'raw_text': raw,
        'bbox': cell.get('bbox'),
        'conf': conf,
        'rule': rule,
        'ambiguous': ambiguous,
        'date': iso,
        'low_confidence': conf < DATE_CONF_FLOOR,
    }
    if iso is None or result['low_confidence'] or ambiguous:
        result['status'] = 'pending_review'
    else:
        result['status'] = 'ok'
    return result


def classify_label(text, conf):
    """Classify a milestone label cell by meaning, never by the word
    'delivery' appearing on a later page."""
    raw = str(text or '').strip()
    if not raw or conf < CELL_CONF_FLOOR:
        return LABEL_UNKNOWN
    if any(word in raw for word in _TRAINING_WORDS):
        return LABEL_TRAINING
    if any(word in raw for word in _ACCEPTANCE_WORDS):
        return LABEL_ACCEPTANCE
    if any(word in raw for word in _DELIVERY_WORDS):
        return LABEL_DELIVERY
    return LABEL_UNKNOWN


def signature_evidence(block):
    """Extract a signature box verdict. A party only counts as signing when
    both the box flag and its OCR confidence clear the gate."""
    parties = []
    for item in block.get('signed', []):
        party = {
            'party': item.get('party'),
            'party_id': item.get('party_id'),
            'signed': bool(item.get('signed')) and float(item.get('conf', 0.0)) >= SIGN_CONF_FLOOR,
            'conf': float(item.get('conf', 0.0)),
            'bbox': item.get('bbox'),
        }
        parties.append(party)
    return {
        'signature_id': block.get('signature_id'),
        'page': block.get('page'),
        'scope': block.get('scope'),
        'parties': parties,
        'dual_signed': len(parties) >= 2 and all(p['signed'] for p in parties),
        'bbox': block.get('bbox'),
    }


def signatures_for(blocks, scope):
    """All signature boxes on this contract whose scope matches a table/amend."""
    out = []
    for block in blocks:
        if block.get('kind') == 'signature' and block.get('scope') == scope:
            out.append(signature_evidence(block))
    return out


def scope_dual_signed(blocks, scope):
    boxes = signatures_for(blocks, scope)
    return any(box['dual_signed'] for box in boxes), boxes


def cluster_columns(header_cells):
    """Cluster a table header into column roles by x-coordinate bands."""
    ordered = sorted(header_cells, key=lambda cell: cell['bbox'][0])
    columns = []
    for cell in ordered:
        text = str(cell.get('text', '')).strip()
        role = 'ignore'
        if any(word in text for word in ('编号', '序号')):
            role = 'item'
        elif '里程碑' in text or '事项' in text or '内容' in text:
            role = 'label'
        elif '日期' in text or '时间' in text:
            role = 'date'
        columns.append({
            'role': role,
            'text': text,
            'bbox': cell.get('bbox'),
            'x0': cell['bbox'][0],
            'x1': cell['bbox'][2],
        })
    return columns


def match_column(columns, cell_bbox):
    """Assign a body cell to a header column by x-band containment, falling
    back to the nearest column center. Continuation rows therefore inherit the
    header of the previous page instead of inventing columns."""
    x0, _y0, x1, _y1 = cell_bbox
    center = (x0 + x1) / 2
    for column in columns:
        if column['x0'] <= x0 and x1 <= column['x1']:
            return column
    return min(columns, key=lambda column: abs(((column['x0'] + column['x1']) / 2) - center))


def detect_missing_pages(blocks):
    """Flag gaps in physical page numbering: a missing scan page may carry the
    table row or signature that decides the delivery date."""
    pages = sorted({int(b['page']) for b in blocks if b.get('page') is not None})
    missing = [page for page in range(pages[0], pages[-1] + 1) if page not in pages] if pages else []
    return pages, missing


def merge_rescans(base_blocks, rescan_blocks):
    """Apply late clear scans by replacing same-id OCR blocks.

    The replaced base block is retained inside the replacement as
    superseded_block so the original evidence and its provenance stay readable.
    Returns (merged_blocks, replacements).
    """
    merged = copy.deepcopy(base_blocks)
    by_id = {block['block_id']: index for index, block in enumerate(merged)}
    replacements = []
    for fresh in rescan_blocks:
        block = copy.deepcopy(fresh)
        original_id = block.get('rescan_of') or block['block_id']
        if original_id in by_id:
            block['superseded_block'] = merged[by_id[original_id]]
            merged[by_id[original_id]] = block
        else:
            by_id[block['block_id']] = len(merged)
            merged.append(block)
        replacements.append({
            'block_id': block['block_id'],
            'page': block.get('page'),
            'rescan_of': original_id,
            'captured_at': block.get('captured_at'),
        })
    return merged, replacements
