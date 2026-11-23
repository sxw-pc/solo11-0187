"""Signed product grants and version conflicts."""
from collections import defaultdict
from .store import canonical

def decide(events):
    groups=defaultdict(list)
    authorities=defaultdict(list)
    for e in events:
        if e['kind']=='contract':groups[(e['tenant'],e['product'])].append(e)
        elif e['kind']=='authority':authorities[e['tenant']].append(e)
    result={}
    for key,rows in groups.items():
        version=max(x['version'] for x in rows)
        same=[x for x in rows if x['version']==version]
        conflict=len({canonical({k:v for k,v in x.items() if k not in ('id','at')}) for x in same})>1
        current=sorted(same,key=lambda x:(x['at'],x['id']))[-1]
        signed=any(a['signer']==current.get('signed_by') and a['start']<=current.get('signed_at','')<a['finish']
                   and a['status']=='active' for a in authorities[current['tenant']])
        valid=current['status']=='active' and signed and not conflict and current['seats']>0 and current['start']<current['finish'] and bool(current['features'])
        result[key]={'event':current,'valid':valid,'conflict':conflict,'seats':current['seats'] if valid else 0,
                     'features':sorted(set(current['features'])) if valid else [],
                     'reason':'version_conflict' if conflict else 'revoked' if current['status']!='active' else
                              'unauthorized_signer' if not signed else 'invalid_terms' if not valid else 'authorized'}
    return result
