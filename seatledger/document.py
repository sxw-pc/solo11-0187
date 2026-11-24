"""Structured causal merge and signable document versions.

Whole-document last-write-wins is replaced by paragraph-level three-way merge
over the parent graph.  Independent edits combine; concurrent edits to the same
amount field stay conflicted and block issuance instead of one side being
dropped by clock or upload order.  Signature attachments are bound to the
content root of the paragraph they reference.  Issued versions are immutable:
late operations only produce new candidate versions and a reconfirmation scope.
"""
from .edit_capture import digest
from .sync_events import edits, lca, tips, topo_order, ROOT_VERSION

ROOT_SNAPSHOT={'paragraphs':{},'order':[],'attachments':{}}


def paragraph_root(snapshot,paragraph_id):
    paragraph=snapshot['paragraphs'][paragraph_id]
    return digest({'paragraph_id':paragraph_id,'fields':paragraph['fields']})


def content_root(snapshot):
    """Content root covering the full body and every attachment binding."""
    body=[]
    for paragraph_id in snapshot['order']:
        body.append({'paragraph_id':paragraph_id,
                     'fields':snapshot['paragraphs'][paragraph_id]['fields'],
                     'root':paragraph_root(snapshot,paragraph_id)})
    attachments=[]
    for attachment_id in sorted(snapshot['attachments']):
        item=snapshot['attachments'][attachment_id]
        attachments.append({'attachment_id':attachment_id,
                            'paragraph_id':item['paragraph_id'],
                            'label':item['label'],'signer':item['signer'],
                            'bound_root':item['root']})
    return digest({'paragraphs':body,'attachments':attachments})


def _clone(snapshot):
    return {'paragraphs':{pid:{'fields':dict(p['fields'])} for pid,p in snapshot['paragraphs'].items()},
            'order':list(snapshot['order']),
            'attachments':{aid:dict(item) for aid,item in snapshot['attachments'].items()}}


def _apply_edit(snapshot,op):
    """Replay one single-parent edit operation onto a cloned snapshot."""
    out=_clone(snapshot)
    body=op['op']
    action=body['action']
    if action=='set_field':
        pid=body['paragraph_id']
        if pid not in out['paragraphs']:
            out['paragraphs'][pid]={'fields':{}}
            out['order'].append(pid)
        out['paragraphs'][pid]['fields'][body['field']]=body['value']
    elif action=='delete_paragraph':
        pid=body['paragraph_id']
        if pid not in out['paragraphs']:
            raise ValueError(f'cannot delete missing paragraph {pid}')
        del out['paragraphs'][pid]
        out['order'].remove(pid)
    elif action=='add_attachment':
        pid=body['paragraph_id']
        if pid not in out['paragraphs']:
            raise ValueError(f'attachment {body["attachment_id"]} references missing paragraph {pid}')
        out['attachments'][body['attachment_id']]={
            'attachment_id':body['attachment_id'],'paragraph_id':pid,
            'label':body.get('label','signature'),'signer':op['author'],
            'root':paragraph_root(out,pid)}
    elif action=='remove_attachment':
        out['attachments'].pop(body['attachment_id'],None)
    elif action=='reconfirm_attachment':
        item=out['attachments'].get(body['attachment_id'])
        if item is None:
            raise ValueError(f'cannot reconfirm missing attachment {body["attachment_id"]}')
        if item['paragraph_id'] not in out['paragraphs']:
            raise ValueError('attachment paragraph is gone; reattach before reconfirming')
        item['signer']=op['author']
        item['root']=paragraph_root(out,item['paragraph_id'])
    return out


def _origin(op):
    return {'origin':op['id'],'device':op['device'],'author':op['author']}


def _conflict_id(doc_id,kind,key):
    return f'{doc_id}:{kind}:{key}'


def _field_three_way(doc_id,pid,field,base,left,right,left_op,right_op):
    b=base.get(field)
    l=left.get(field)
    r=right.get(field)
    if l==r:
        return l,None
    if l==b:
        return r,None
    if r==b:
        return l,None
    return b,{'conflict_id':_conflict_id(doc_id,'field',f'{pid}:{field}'),
              'kind':'field','paragraph_id':pid,'field':field,'base':b,
              'choices':[{'value':l,**_origin(left_op)},
                         {'value':r,**_origin(right_op)}]}


def three_way_merge(doc_id,base,left,right,left_op,right_op):
    """Paragraph/attachment three-way merge over a common base snapshot.

    Returns the merged snapshot plus explicit conflicts; nothing is decided by
    clock or dictionary order.
    """
    merged=_clone(base)
    conflicts=[]
    base_ids=set(base['paragraphs'])
    left_ids=set(left['paragraphs'])
    right_ids=set(right['paragraphs'])
    ordered=list(base['order'])
    for pid in sorted((left_ids|right_ids)-base_ids):
        if pid not in ordered:
            ordered.append(pid)
    merged['order']=ordered
    for pid in ordered:
        in_b,in_l,in_r=pid in base_ids,pid in left_ids,pid in right_ids
        if in_l and not in_r:
            if in_b and not in_r:           # right deleted, left kept
                if in_l and left['paragraphs'][pid]['fields']!=base['paragraphs'][pid]['fields']:
                    conflicts.append({'conflict_id':_conflict_id(doc_id,'delete_edit',pid),
                                      'kind':'delete_edit','paragraph_id':pid,
                                      'choices':[{'value':'keep',**_origin(left_op)},
                                                 {'value':'delete',**_origin(right_op)}]})
                    merged['paragraphs'][pid]={'fields':dict(left['paragraphs'][pid]['fields'])}
                    _flag_pending_dangling(doc_id,merged,conflicts,pid,left_op,right_op)
                continue
            merged['paragraphs'][pid]={'fields':dict(left['paragraphs'][pid]['fields'])}
            continue
        if in_r and not in_l:
            if in_b and not in_l and right['paragraphs'][pid]['fields']!=base['paragraphs'][pid]['fields']:
                conflicts.append({'conflict_id':_conflict_id(doc_id,'delete_edit',pid),
                                  'kind':'delete_edit','paragraph_id':pid,
                                  'choices':[{'value':'delete',**_origin(left_op)},
                                             {'value':'keep',**_origin(right_op)}]})
                merged['paragraphs'][pid]={'fields':dict(right['paragraphs'][pid]['fields'])}
                _flag_pending_dangling(doc_id,merged,conflicts,pid,left_op,right_op)
                continue
            if not in_b:
                merged['paragraphs'][pid]={'fields':dict(right['paragraphs'][pid]['fields'])}
            continue
        if not in_l and not in_r:
            merged['paragraphs'].pop(pid,None)
            continue
        merged['paragraphs'].setdefault(pid,{'fields':{}})
        fields=set(base['paragraphs'].get(pid,{'fields':{}})['fields'])
        fields|=set(left['paragraphs'][pid]['fields'])|set(right['paragraphs'][pid]['fields'])
        for field in sorted(fields):
            value,conflict=_field_three_way(
                doc_id,pid,field,
                base['paragraphs'].get(pid,{'fields':{}})['fields'],
                left['paragraphs'][pid]['fields'],right['paragraphs'][pid]['fields'],
                left_op,right_op)
            merged['paragraphs'][pid]['fields'][field]=value
            if conflict:conflicts.append(conflict)
    merged['order']=[pid for pid in merged['order'] if pid in merged['paragraphs']]
    merged,attachment_conflicts=_merge_attachments(doc_id,base,left,right,merged,left_op,right_op)
    conflicts.extend(attachment_conflicts)
    return merged,conflicts


def _flag_pending_dangling(doc_id,merged,conflicts,pid,left_op,right_op):
    """Flag attachments bound to a paragraph a branch deleted.

    The paragraph is kept in the merged body pending the delete_edit ruling,
    but the signature references cannot survive a 'delete' ruling: they are
    adjudicated together with the paragraph instead of being left behind.
    """
    existing={c['attachment_id'] for c in conflicts if c['kind']=='dangling_attachment'}
    for aid,item in sorted(merged['attachments'].items()):
        if item['paragraph_id']==pid and aid not in existing:
            conflicts.append({'conflict_id':_conflict_id(doc_id,'dangling_attachment',aid),
                              'kind':'dangling_attachment','attachment_id':aid,
                              'paragraph_id':pid,
                              'choices':[{'value':'detach',**_origin(left_op)},
                                         {'value':'reattach',**_origin(right_op)}]})


def _merge_attachments(doc_id,base,left,right,merged,left_op,right_op):
    conflicts=[]
    base_a=base['attachments'];left_a=left['attachments'];right_a=right['attachments']
    ids=sorted(set(base_a)|set(left_a)|set(right_a))
    merged['attachments']={}
    for aid in ids:
        b,l,r=base_a.get(aid),left_a.get(aid),right_a.get(aid)
        if l is None and r is None:
            continue
        if l is None:
            if b is not None:                       # left removed it
                if r==b:
                    continue                        # clean removal
                conflicts.append({'conflict_id':_conflict_id(doc_id,'attachment_remove',aid),
                                  'kind':'attachment_remove','attachment_id':aid,
                                  'paragraph_id':r['paragraph_id'],
                                  'choices':[{'value':'remove',**_origin(left_op)},
                                             {'value':'keep',**_origin(right_op)}]})
            merged['attachments'][aid]=dict(r)
            continue
        if r is None:
            if b is not None:
                if l==b:
                    continue
                conflicts.append({'conflict_id':_conflict_id(doc_id,'attachment_remove',aid),
                                  'kind':'attachment_remove','attachment_id':aid,
                                  'paragraph_id':l['paragraph_id'],
                                  'choices':[{'value':'keep',**_origin(left_op)},
                                             {'value':'remove',**_origin(right_op)}]})
            merged['attachments'][aid]=dict(l)
            continue
        if b is None:                               # added on both branches
            if l==r:
                merged['attachments'][aid]=dict(l)
            else:
                conflicts.append({'conflict_id':_conflict_id(doc_id,'attachment_add',aid),
                                  'kind':'attachment_add','attachment_id':aid,
                                  'paragraph_id':l['paragraph_id'],
                                  'choices':[{'value':l,**_origin(left_op)},
                                             {'value':r,**_origin(right_op)}]})
                merged['attachments'][aid]=dict(l)
            continue
        if l['root']==r['root']:
            merged['attachments'][aid]=dict(l)
        elif l['root']==b['root']:
            merged['attachments'][aid]=dict(r)
        elif r['root']==b['root']:
            merged['attachments'][aid]=dict(l)
        else:
            conflicts.append({'conflict_id':_conflict_id(doc_id,'attachment_root',aid),
                              'kind':'attachment_root','attachment_id':aid,
                              'paragraph_id':l['paragraph_id'],
                              'choices':[{'value':l['root'],**_origin(left_op)},
                                         {'value':r['root'],**_origin(right_op)}]})
            merged['attachments'][aid]=dict(l)
    # Defensive: any remaining attachment pointing at absent body text is
    # dangling regardless of how the paragraph disappeared.
    known_dangling={c['attachment_id'] for c in conflicts if c['kind']=='dangling_attachment'}
    for aid,item in sorted(merged['attachments'].items()):
        if item['paragraph_id'] not in merged['paragraphs'] and aid not in known_dangling:
            conflicts.append({'conflict_id':_conflict_id(doc_id,'dangling_attachment',aid),
                              'kind':'dangling_attachment','attachment_id':aid,
                              'paragraph_id':item['paragraph_id'],
                              'choices':[{'value':'detach',**_origin(left_op)},
                                         {'value':'reattach',**_origin(left_op)}]})
    return merged,conflicts


def _stale_attachments(snapshot):
    stale=[]
    for aid,item in sorted(snapshot['attachments'].items()):
        paragraph=snapshot['paragraphs'].get(item['paragraph_id'])
        if paragraph is None:
            continue
        current=paragraph_root(snapshot,item['paragraph_id'])
        if item['root']!=current:
            stale.append({'attachment_id':aid,'signer':item['signer'],
                          'paragraph_id':item['paragraph_id'],
                          'bound_root':item['root'],'current_root':current})
    return stale


def _valid_choice(conflict,value):
    return any(choice['value']==value for choice in conflict['choices'])


def _apply_resolutions(snapshot,conflicts,decisions):
    by_id={c['conflict_id']:c for c in conflicts}
    unknown=[d['conflict_id'] for d in decisions if d['conflict_id'] not in by_id]
    if unknown:
        raise ValueError(f'resolution targets unknown conflict: {unknown[0]}')
    # Paragraph adjudications first: whether the body survives decides whether
    # its attachment references are still dangling.
    ordered=sorted(conflicts,key=lambda c:c['kind']=='dangling_attachment')
    remaining=[]
    for conflict in ordered:
        cid=conflict['conflict_id']
        decision=next((d for d in decisions if d['conflict_id']==cid),None)
        if conflict['kind']=='dangling_attachment':
            # Restoring the paragraph re-anchors the attachment automatically;
            # a still-missing paragraph forces the attachment to be detached
            # rather than leaving a signature at nonexistent body text.
            if conflict['paragraph_id'] in snapshot['paragraphs']:
                continue
            if decision is not None and not _valid_choice(conflict,decision['value']):
                raise ValueError(f'resolution value not among retained choices for {cid}')
            snapshot['attachments'].pop(conflict['attachment_id'],None)
            continue
        if decision is None:
            remaining.append(conflict)
            continue
        if not _valid_choice(conflict,decision['value']):
            raise ValueError(f'resolution value not among retained choices for {cid}')
        kind=conflict['kind']
        if kind=='field':
            snapshot['paragraphs'][conflict['paragraph_id']]['fields'][conflict['field']]=decision['value']
        elif kind=='delete_edit':
            pid=conflict['paragraph_id']
            if decision['value']=='delete':
                snapshot['paragraphs'].pop(pid,None)
                if pid in snapshot['order']:
                    snapshot['order'].remove(pid)
            # 'keep' leaves the already-merged paragraph in place
        elif kind in ('attachment_remove','attachment_add','attachment_root'):
            if decision['value'] in ('remove','detach'):
                snapshot['attachments'].pop(conflict['attachment_id'],None)
            # keep/attach/reattach leaves the attachment in place for reconfirmation
        else:
            remaining.append(conflict)
    return remaining


def render(snapshot,conflicts):
    """Business-facing body text; conflicts render as explicit blocks."""
    lines=[]
    conflict_fields={(c['paragraph_id'],c['field']) for c in conflicts if c['kind']=='field'}
    for pid in snapshot['order']:
        fields=snapshot['paragraphs'][pid]['fields']
        for field in sorted(fields):
            if (pid,field) in conflict_fields:
                match=next(c for c in conflicts if c['kind']=='field' and c['paragraph_id']==pid and c['field']==field)
                rendered=' / '.join(f"[待裁定 {choice['device']}:{choice['value']}]" for choice in match['choices'])
                lines.append(f'{pid}.{field}: {rendered}')
            else:
                lines.append(f'{pid}.{field}: {fields[field]}')
    for conflict in conflicts:
        if conflict['kind']!='field':
            lines.append(f"冲突 {conflict['conflict_id']} 待共同签认")
    for aid,item in sorted(snapshot['attachments'].items()):
        lines.append(f"附件 {aid} -> {item['paragraph_id']} (签署人 {item['signer']}, 根 {item['root'][:12]})")
    return '\n'.join(lines)


def replay(ops):
    """Idempotently replay a document's ops; retain every version snapshot."""
    ordered=topo_order(ops)
    snapshots={ROOT_VERSION:ROOT_SNAPSHOT}
    issued=[]
    candidate_conflicts={}
    for op in ordered:
        parents=op['parents']
        action=op['op']['action']
        if action in ('resolve','issue'):
            if len(parents)<2 and action=='resolve':
                raise ValueError('resolve must adjudicate at least two parent branches')
            merged=_clone(snapshots[parents[0]])
            merged_conflicts=list(candidate_conflicts.get(parents[0],[]))
            for parent in parents[1:]:
                base_id=lca(ops,parents[0],parent)
                base=snapshots[base_id] if base_id else ROOT_SNAPSHOT
                merged,new_conflicts=three_way_merge(
                    op['document_id'],base,merged,snapshots[parent],
                    _merge_anchor(ops,parents[0]),op)
                merged_conflicts=[c for c in merged_conflicts if c not in new_conflicts]+new_conflicts
            if action=='resolve':
                merged_conflicts=_apply_resolutions(merged,merged_conflicts,op['op']['decisions'])
                candidate_conflicts[op['id']]=merged_conflicts
            else:
                evaluation=_evaluate_snapshot(op['document_id'],merged,merged_conflicts)
                if evaluation['conflicts'] or evaluation['stale']:
                    raise ValueError('cannot issue a version with unresolved conflicts or stale attachments')
                sealed={'version_id':op['id'],'label':op['op'].get('version_label','sealed'),
                        'device':op['device'],'clock':op['clock'],'root':content_root(merged),
                        'snapshot':merged,'approvals':op['op'].get('approvals',[])}
                issued.append(sealed)
                candidate_conflicts[op['id']]=merged_conflicts
            snapshots[op['id']]=merged
            continue
        if len(parents)>1:
            raise ValueError(f"edit {op['id']} cannot carry multiple parents; use resolve")
        parent=parents[0] if parents else ROOT_VERSION
        if parent not in snapshots:
            raise ValueError(f"unknown parent version {parent}")
        snapshots[op['id']]=_apply_edit(snapshots[parent],op)
        candidate_conflicts[op['id']]=list(candidate_conflicts.get(parent,[]))
    return ordered,snapshots,issued,candidate_conflicts


def _merge_anchor(ops,version_id):
    return next(op for op in ops if op['id']==version_id)


def _evaluate_snapshot(doc_id,snapshot,known_conflicts):
    conflicts=[dict(c) for c in known_conflicts]
    known={c['conflict_id'] for c in conflicts}
    for aid,item in sorted(snapshot['attachments'].items()):
        if item['paragraph_id'] not in snapshot['paragraphs']:
            cid=_conflict_id(doc_id,'dangling_attachment',aid)
            if cid not in known:
                conflicts.append({'conflict_id':cid,'kind':'dangling_attachment',
                                  'attachment_id':aid,'paragraph_id':item['paragraph_id'],
                                  'choices':[{'value':'detach','origin':None,'device':None},
                                             {'value':'reattach','origin':None,'device':None}]})
    stale=_stale_attachments(snapshot)
    root=content_root(snapshot)
    return {'snapshot':snapshot,'conflicts':conflicts,'stale':stale,'root':root,
            'text':render(snapshot,conflicts)}


def evaluate(ops):
    """Full causal evaluation for one document's normalized operations."""
    ordered,snapshots,issued,candidate_conflicts=replay(ops)
    doc_id=ordered[0]['document_id']
    heads=tips(ordered)
    if len(heads)==1:
        head=heads[0]
        candidate=_evaluate_snapshot(doc_id,snapshots[head],candidate_conflicts.get(head,[]))
    else:
        if len(heads)!=2:
            raise ValueError('exactly two open branches can be auto-merged; use a resolve operation')
        merged=_clone(snapshots[heads[0]])
        conflicts=list(candidate_conflicts.get(heads[0],[]))
        for head in heads[1:]:
            base_id=lca(ordered,heads[0],head)
            base=snapshots[base_id] if base_id else ROOT_SNAPSHOT
            merged,new_conflicts=three_way_merge(
                doc_id,base,merged,snapshots[head],
                _merge_anchor(ordered,heads[0]),_merge_anchor(ordered,head))
            conflicts=[c for c in conflicts if c not in new_conflicts]+new_conflicts
        candidate=_evaluate_snapshot(doc_id,merged,conflicts)
    candidate['tips']=heads
    candidate['versions']={eid:{'root':content_root(snap),'snapshot':snap}
                           for eid,snap in snapshots.items() if eid!=ROOT_VERSION}
    if issued:
        status='issued'
    elif candidate['conflicts']:
        status='conflicted'
    elif candidate['stale']:
        status='stale_attachments'
    else:
        status='ready_to_issue'
    candidate['status']=status
    candidate['issued']=[{'version_id':v['version_id'],'label':v['label'],
                          'root':v['root'],'clock':v['clock'],'device':v['device'],
                          'snapshot':v['snapshot']} for v in issued]
    return candidate


def text(events):
    """Per-document structured merge result with the signable body text."""
    return {doc_id:evaluate(rows) for doc_id,rows in edits(events).items()}
