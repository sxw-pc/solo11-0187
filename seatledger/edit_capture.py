"""Capture offline document edits as structured, causally parented operations.

A device never decides ordering between mutually-offline edits with its local
clock: every edit names the parent version it was based on.  The captured
operation keeps the clock only as provenance; causality is reconstructed from
the parent graph in :mod:`seatledger.sync_events`.
"""
import hashlib
import json

ACTIONS=frozenset((
    'set_field','delete_paragraph','add_attachment','remove_attachment',
    'reconfirm_attachment','resolve','issue',
))
# Merge-style actions that resolve more than one parent tip.
MERGE_ACTIONS=frozenset(('resolve','issue'))


def canonical(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def digest(obj):
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()


def _require(value,label):
    if not isinstance(value,str) or not value:
        raise ValueError(f'{label} is required')
    return value


def normalize_event(event):
    """Normalize one raw device event into a structured operation.

    Legacy whole-document snapshots (``content`` without ``action``) become a
    single ``set_field`` edit against an implicit seed paragraph.  Two such
    snapshots with no declared parent are therefore genuinely concurrent:
    neither clock nor upload order can make one the successor of the other.
    """
    if not isinstance(event,dict):
        raise ValueError('edit event must be an object')
    doc_id=_require(event.get('document_id'),'document_id')
    device=_require(event.get('device'),'device')
    if 'clock' not in event or not isinstance(event['clock'],int):
        raise ValueError('integer clock is required as provenance')
    event_id=event.get('id') or f'{doc_id}:{device}:{event["clock"]}'
    _require(event_id,'event id')
    parents=event.get('parents')
    if parents is None:
        parents=[] if event.get('parent') is None else [event['parent']]
    if not isinstance(parents,list) or any(not isinstance(p,str) or not p for p in parents):
        raise ValueError('parents must be a list of version ids')
    action=event.get('action')
    op={'id':event_id,'document_id':doc_id,'device':device,
        'author':event.get('author',device),'clock':event['clock'],
        'parents':list(parents),'op':{}}
    if action is None:
        if not isinstance(event.get('content'),str):
            raise ValueError('legacy event content must be text')
        op['op']={'action':'set_field','paragraph_id':'p_body','field':'text',
                  'value':event['content']}
        return op
    if action not in ACTIONS:
        raise ValueError(f'unsupported action: {action}')
    if action in MERGE_ACTIONS and not parents:
        raise ValueError(f'{action} requires the parent branches it adjudicates')
    body=dict(event)
    for key in ('document_id','device','clock','id','parent','parents','action','author'):
        body.pop(key,None)
    body['action']=action
    if action=='set_field':
        _require(body.get('paragraph_id'),'paragraph_id')
        _require(body.get('field'),'field')
        if 'value' not in body:
            raise ValueError('set_field requires a value')
    elif action=='delete_paragraph':
        _require(body.get('paragraph_id'),'paragraph_id')
    elif action=='add_attachment':
        _require(body.get('attachment_id'),'attachment_id')
        _require(body.get('paragraph_id'),'paragraph_id')
        body.setdefault('label','signature')
    elif action in ('remove_attachment','reconfirm_attachment'):
        _require(body.get('attachment_id'),'attachment_id')
    elif action=='resolve':
        decisions=body.get('decisions')
        if not isinstance(decisions,list) or not decisions:
            raise ValueError('resolve requires nonempty decisions')
        _require_joint_approvals(body)
        for d in decisions:
            if not isinstance(d,dict) or not d.get('conflict_id') or 'value' not in d:
                raise ValueError('each resolution decision needs conflict_id and value')
    elif action=='issue':
        _require_joint_approvals(body)
        body.setdefault('version_label','sealed')
    op['op']=body
    return op


def _require_joint_approvals(body):
    approvals=body.get('approvals')
    if not isinstance(approvals,list) or not approvals:
        raise ValueError('joint sign-off requires approvals')
    roles={a.get('role') for a in approvals if isinstance(a,dict)}
    if 'customer' not in roles or 'consultant' not in roles:
        raise ValueError('joint sign-off requires both customer and consultant')


def capture(events):
    """Idempotently normalize device events.

    Replaying the same event id yields the same normalized operation and is
    counted as a replay; the same id with different content is a collision,
    mirroring the evidence ledger's replay/collision rule.  No clock-based
    ordering happens here.
    """
    if not isinstance(events,list) or not events:
        raise ValueError('nonempty edit event list required')
    normalized={}
    order=[]
    inserted=replayed=0
    for raw in events:
        op=normalize_event(raw)
        old=normalized.get(op['id'])
        if old is not None:
            if old != op:
                raise ValueError('edit id content collision')
            replayed+=1
            continue
        normalized[op['id']]=op
        order.append(op['id'])
        inserted+=1
    return {'events':[normalized[i] for i in order],
            'inserted':inserted,'replayed':replayed}
