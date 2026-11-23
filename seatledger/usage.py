"""Feature authorization, session overlap, and true-up liability."""
from collections import defaultdict

def decide(events,grants,leases):
    sessions=[e for e in events if e['kind']=='session'];out={};groups=defaultdict(list)
    contracts=[e for e in events if e['kind']=='contract']
    def terms(key,at):
        rows=[x for x in contracts if (x['tenant'],x['product'])==key and x['at']<=at]
        return sorted(rows,key=lambda x:(x['version'],x['at'],x['id']))[-1] if rows else None
    for e in sessions:
        key=(e['tenant'],e['product']);grant=grants.get(key)
        historical=terms(key,e['start'])
        lease=next((l for l in leases.values() if l['valid'] and l['event']['tenant']==e['tenant'] and l['event']['product']==e['product'] and l['device']==e['device'] and l['event']['start']<=e['start']<e['finish']<=l['finish']),None)
        reason=('no_grant' if not grant or not grant['valid'] or not historical or historical['status']!='active' else
                'feature_violation' if e['feature'] not in historical['features'] else
                'outside_contract' if not(historical['start']<=e['start']<e['finish']<=historical['finish']) else
                'lease_missing_or_expired' if not lease else 'compliant')
        out[e['id']]={'status':reason,'device':e['device'],'feature':e['feature'],'start':e['start'],'finish':e['finish']}
        if reason=='compliant':groups[key].append(e)
    settlements={}
    for key,grant in grants.items():
        times=sorted({t for e in groups[key] for t in (e['start'],e['finish'])})
        peak=max((len({e['device'] for e in groups[key] if e['start']<=t<e['finish']}) for t in times),default=0)
        over=max((max(0,len({e['device'] for e in groups[key] if e['start']<=t<e['finish']})-
                      (terms(key,t)['seats'] if terms(key,t) else 0)) for t in times),default=0)
        tenant,product=key
        settlements[f'settlement:{tenant}:{product}']={'peak':peak,'seats':grant['seats'],'overage':over,'amount':over*10000,
                                                          'status':'overage_due' if over else 'balanced',
                                                          'feature_violations':sum(1 for e in sessions if (e['tenant'],e['product'])==key and out[e['id']]['status']=='feature_violation')}
    return out,settlements
