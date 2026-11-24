"""Collect offline document edits and reconstruct causality from parents.

Local clocks are never compared to order mutually-offline edits.  The parent
graph alone decides whether one edit is a causal successor of another or a
genuinely concurrent branch.
"""
from .edit_capture import capture, normalize_event

ROOT_VERSION='__root__'


def edits(events):
    """Group normalized edit operations by document id."""
    if events and isinstance(events[0],dict) and 'op' not in events[0]:
        events=capture(events)['events']
    result={}
    for op in events:
        result.setdefault(op['document_id'],[]).append(op)
    return result


def _index(ops):
    by_id={op['id']:op for op in ops}
    if len(by_id)!=len(ops):
        raise ValueError('duplicate edit id within document')
    return by_id


def topo_order(ops):
    """Parent-before-child topological order.

    Ties (truly concurrent operations) break on event id only for stable
    replay; clock values never participate.  Unknown parents and cycles are
    rejected instead of guessed at.
    """
    by_id=_index(ops)
    indegree={eid:0 for eid in by_id}
    children={eid:[] for eid in by_id}
    for op in ops:
        for parent in op['parents']:
            if parent not in by_id:
                raise ValueError(f"unknown parent version {parent} of {op['id']}")
            indegree[op['id']]+=1
            children[parent].append(op['id'])
    ready=sorted(eid for eid,deg in indegree.items() if deg==0)
    out=[]
    while ready:
        eid=ready.pop(0)
        out.append(by_id[eid])
        for child in sorted(children[eid]):
            indegree[child]-=1
            if indegree[child]==0:
                ready.append(child)
        ready.sort()
    if len(out)!=len(by_id):
        raise ValueError('cyclic parent graph cannot be replayed')
    return out


def ancestor_sets(ops):
    """Map each version id to the set of its strict causal ancestors."""
    ancestors={}
    for op in topo_order(ops):
        seen=set()
        for parent in op['parents']:
            seen.add(parent)
            seen.update(ancestors[parent])
        ancestors[op['id']]=seen
    return ancestors


def relation(ops,left_id,right_id):
    """Classify two versions via the parent graph, never via clocks."""
    ancestors=ancestor_sets(ops)
    if right_id in ancestors.get(left_id,()):
        return 'successor'          # left was built on right
    if left_id in ancestors.get(right_id,()):
        return 'predecessor'
    return 'concurrent'


def lca(ops,left_id,right_id):
    """Lowest common ancestor version of two branch tips (``None`` at root)."""
    ancestors=ancestor_sets(ops)
    common=ancestors[left_id] & ancestors[right_id]
    if not common:
        return None
    deepest=max(common,key=lambda eid:len(ancestors[eid]))
    return deepest


def tips(ops):
    """Version ids no other version names as parent: the open branch heads."""
    referenced={p for op in ops for p in op['parents']}
    return [op['id'] for op in ops if op['id'] not in referenced]


def concurrent_groups(ops):
    """Pairs of tip versions that are genuinely concurrent (same LCA)."""
    by_id=_index(ops)
    heads=tips(ops)
    groups=[]
    for i,a in enumerate(heads):
        for b in heads[i+1:]:
            if relation(ops,a,b)=='concurrent':
                groups.append({'lca':lca(ops,a,b),'left':a,'right':b,
                               'left_device':by_id[a]['device'],
                               'right_device':by_id[b]['device']})
    return groups
