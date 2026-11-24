"""Composite-key migration planning for projects, contacts and approval links.

Hard rules implemented here:

1. identity = (source_system, tenant, source_no); same number in another
   tenant never overwrites a row;
2. owner/approver/alias references must resolve to one imported real subject;
   an unproven many-to-one claim freezes the affected relation instead of
   guessing an owner;
3. projects and their approval edges are produced in one readable version, so
   a project can never be published before its approver edge exists;
4. planning is pure and side-effect free; the append-only mapping ledger and
   scoped revision lists live in :mod:`seatledger.import_capture`.
"""
from .import_rows import rows as collect_rows, ref_rid, key_text
from .store import digest

GOVERNANCE_KINDS = ('identity_claim', 'identity_mapping', 'identity_separation')
CONTACT = 'contact'
PROJECT = 'project'


class MigrationError(ValueError):
    """A batch that cannot be committed as one version."""


def count(events):
    """Migratable business rows: one per composite identity, no overwrite."""
    return len(collect_rows(events))


def split_events(events):
    business, governance = [], []
    for event in events:
        if event.get('kind') in GOVERNANCE_KINDS or 'external_id' not in event:
            governance.append(event)
        else:
            business.append(event)
    return business, governance


class _DSU:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic subject id: smallest rid in the component.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra
        return self.find(a)


def _topo_order(contact_rids, alias_targets):
    """Kahn order over alias edges; cycles keep staged rids for later passes."""
    incoming = {r: 0 for r in contact_rids}
    adjacency = {r: [] for r in contact_rids}
    for src, dst in alias_targets:
        adjacency[src].append(dst)
        incoming[dst] += 1
    ready = sorted(r for r, degree in incoming.items() if degree == 0)
    order, staged = [], []
    seen = set()
    while ready:
        node = ready.pop(0)
        if node in seen:
            continue
        seen.add(node)
        order.append(node)
        for nxt in adjacency[node]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    for r in sorted(contact_rids):
        if r not in seen:
            staged.append(r)  # part of an alias cycle: needs a staged id
    return order, staged


def _claim_index(governance):
    """claim_id -> latest governing document (by at, then event order)."""
    index = {}
    for order, doc in enumerate(governance):
        claim_id = doc.get('claim_id')
        if not claim_id:
            continue
        old = index.get(claim_id)
        if old is None or (doc.get('at', ''), order) >= (old[0].get('at', ''), old[1]):
            index[claim_id] = (doc, order)
    return {claim_id: doc for claim_id, (doc, _) in index.items()}


def _governor_for(docs_by_claim, pair):
    """Latest signed mapping/separation applicable to one candidate pair."""
    pair_set = set(pair)
    found = None
    for doc in docs_by_claim.values():
        members = {ref_rid(m) for m in doc.get('members', [])}
        kind = doc.get('kind')
        applies = ((kind == 'identity_mapping' and pair_set <= members)
                   or (kind == 'identity_separation' and bool(pair_set & members)))
        if applies and (found is None or doc.get('at', '') >= found.get('at', '')):
            found = doc
    return found


def plan(events):
    """Build one atomic migration version from an offline export batch."""
    business, governance = split_events(events)
    by_rid = {key_text(event): event for event in collect_rows(business).values()}
    rids = set(by_rid)
    contacts = sorted(r for r, e in by_rid.items() if e['kind'] == CONTACT)
    projects = sorted(r for r, e in by_rid.items() if e['kind'] == PROJECT)
    unknown = sorted({e['kind'] for e in by_rid.values()} - {CONTACT, PROJECT})
    if unknown:
        raise MigrationError(f'unknown business kind {unknown!r}')

    # --- validate every reference before any subject is materialized -------
    alias_targets, project_refs = [], []
    for rid in contacts:
        for alias in by_rid[rid].get('aliases', []):
            target = ref_rid(alias)
            if target not in rids or by_rid[target]['kind'] != CONTACT:
                raise MigrationError(f'{rid} aliases missing contact {target}')
            if target != rid:
                alias_targets.append((rid, target))
    for rid in projects:
        event = by_rid[rid]
        for field in ('owner_ref', 'approver_ref'):
            if field in event and event[field] is not None:
                target = ref_rid(event[field])
                if target not in rids or by_rid[target]['kind'] != CONTACT:
                    raise MigrationError(f'{rid} {field} resolves to no contact: {target}')
                project_refs.append((rid, field, target))

    # --- provenance of cross-subject claims --------------------------------
    docs_by_claim = _claim_index(governance)
    raw_claims = [d for d in governance if d.get('kind') == 'identity_claim']
    pair_claim = {}
    for doc in raw_claims:
        members = [ref_rid(m) for m in doc.get('members', [])]
        for member in members:
            if member not in rids or by_rid[member]['kind'] != CONTACT:
                raise MigrationError(f'claim {doc.get("claim_id")} member missing: {member}')
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = tuple(sorted((a, b)))
                old = pair_claim.get(pair)
                if old is None or doc.get('at', '') >= old.get('at', ''):
                    pair_claim[pair] = doc

    # --- identity graph: merge only what is provably the same real subject -
    dsu = _DSU(rids)
    frozen = []

    def freeze(rid_pair, reason, claim_id=None):
        entry = {'relation': 'alias', 'from': rid_pair[0], 'to': rid_pair[1],
                 'reason': reason, 'claim_id': claim_id}
        if entry not in frozen:
            frozen.append(entry)

    inline_pairs = {tuple(sorted(p)) for p in alias_targets}
    candidates = set(inline_pairs) | set(pair_claim)
    for pair in sorted(candidates):
        if pair[0] not in rids or pair[1] not in rids:
            continue
        inline = pair in inline_pairs
        tenants = {by_rid[pair[0]]['tenant'], by_rid[pair[1]]['tenant']}
        claim = pair_claim.get(pair)
        governor = _governor_for(docs_by_claim, pair)

        if governor is not None and governor.get('kind') == 'identity_separation':
            freeze(pair, 'identity_separated', governor.get('claim_id'))
            continue
        if len(tenants) == 1 and inline:
            # inline same-tenant aliases inside one export are source-proven
            dsu.union(*pair)
            continue
        if governor is None or governor.get('kind') != 'identity_mapping':
            # a bare identity_claim is an assertion, not proof of a subject
            freeze(pair, 'unproven_many_to_one',
                   (governor or claim or {}).get('claim_id'))
            continue
        if tenants <= set(governor.get('signed_by', [])):
            dsu.union(*pair)  # every affected tenant signed the common subject
        else:
            freeze(pair, 'cross_tenant_unauthorized', governor.get('claim_id'))

    # --- topological import with staged ids for alias cycles ---------------
    topo, staged_rids = _topo_order(contacts, alias_targets)
    staged_ids = {rid: f'staged:{rid}' for rid in staged_rids}

    # --- materialized subjects ---------------------------------------------
    subjects, contact_subject = {}, {}
    for rid in rids:
        root = dsu.find(rid)
        if root not in subjects:
            subjects[root] = {'subject_id': f'S:{root}', 'rids': [], 'kind': by_rid[root]['kind'],
                              'status': 'active'}
        subjects[root]['rids'].append(rid)
        contact_subject[rid] = subjects[root]['subject_id']
    for entry in frozen:
        for rid in (entry['from'], entry['to']):
            subjects[dsu.find(rid)]['status'] = 'frozen_relation'

    # --- relations: aliases and project owner/approver edges in one version -
    relations = []
    for src, dst in sorted(alias_targets):
        linked = dsu.find(src) == dsu.find(dst)
        relations.append({'relation': 'alias', 'from': src, 'to': dst,
                          'from_subject': contact_subject[src], 'to_subject': contact_subject[dst],
                          'status': 'linked' if linked else 'frozen'})
    for rid, field, target in project_refs:
        relations.append({'relation': field.replace('_ref', ''), 'from': rid, 'to': target,
                          'from_subject': f'P:{rid}', 'to_subject': contact_subject[target],
                          'status': 'linked'})

    # --- customer-facing mapping rows, one per source row ------------------
    mappings = []
    for rid in sorted(rids):
        event = by_rid[rid]
        row = {'rid': rid, 'source_system': event.get('source_system', 'legacy'),
               'tenant': event['tenant'], 'source_no': str(event.get('source_no', event['external_id'])),
               'kind': event['kind'], 'subject_id': contact_subject[rid]
               if event['kind'] == CONTACT else f'P:{rid}',
               'status': subjects[dsu.find(rid)]['status'] if event['kind'] == CONTACT else 'active',
               'staged_id': staged_ids.get(rid)}
        mappings.append(row)

    names = {rid: by_rid[rid].get('name') for rid in contacts}
    result = {
        'version': None,  # assigned by the capture ledger at commit time
        'business_row_count': len(by_rid),
        'records': [{'rid': rid, **{k: v for k, v in by_rid[rid].items() if k != 'external_id'}}
                    for rid in sorted(rids)],
        'topo_order': topo,
        'staged_ids': staged_ids,
        'subjects': [subjects[k] for k in sorted(subjects)],
        'relations': relations,
        'frozen': frozen,
        'mappings': mappings,
        'same_name_kept_distinct': sorted(
            (a, b) for i, a in enumerate(contacts) for b in contacts[i + 1:]
            if names.get(a) and names.get(a) == names.get(b)
            and contact_subject[a] != contact_subject[b]),
    }
    result['digest'] = digest({k: v for k, v in result.items() if k not in ('version', 'digest')})
    return result


def affected_scope(manifest, changed_rids):
    """Dependency-located revision scope: changed rows plus everything referencing them."""
    changed = {ref_rid(r) for r in changed_rids}
    scope = set(changed)
    for relation in manifest['relations']:
        if relation['to'] in changed or relation['from'] in changed:
            scope.update((relation['from'], relation['to']))
    for entry in manifest['frozen']:
        if entry['from'] in changed or entry['to'] in changed:
            scope.update((entry['from'], entry['to']))
    return sorted(scope)


def revision_list(old_manifest, new_manifest, changed_rids):
    """Scoped, non-destructive revision list produced by a late source correction.

    Governance documents carry no source row, so the seed set can be empty;
    the rows whose mapping actually changed (and their dependents) are pulled
    into the dependency-located scope as well.
    """
    old_rows = {row['rid']: row for row in old_manifest['mappings']}
    diff_rids = {row['rid'] for row in new_manifest['mappings']
                 if old_rows.get(row['rid']) != row}
    seed = {ref_rid(r) for r in changed_rids} | diff_rids
    scope = set(affected_scope(new_manifest, seed))
    items = []
    for row in new_manifest['mappings']:
        if row['rid'] not in scope:
            continue
        before = old_rows.get(row['rid'])
        if before != row:
            items.append({'rid': row['rid'], 'before': before, 'after': row})
    return {'changed_rids': sorted(ref_rid(r) for r in changed_rids),
            'scope': sorted(scope), 'items': items,
            'old_version': old_manifest['version'], 'new_version': new_manifest['version']}
