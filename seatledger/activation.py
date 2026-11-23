"""Device identity, seat lease, transfer, and offline expiration."""
from collections import defaultdict

def decide(events,grants):
    devices=defaultdict(list);leases=[];transfers=[]
    for e in events:
        if e['kind']=='device':devices[(e['tenant'],e['device'])].append(e)
        elif e['kind']=='lease':leases.append(e)
        elif e['kind']=='transfer':transfers.append(e)
    current={k:sorted(v,key=lambda x:(x['at'],x['id']))[-1] for k,v in devices.items()}
    fingerprints=defaultdict(set)
    for (tenant,device),e in current.items():fingerprints[(tenant,e['fingerprint'])].add(device)
    result={};allocated=defaultdict(list)
    for e in sorted(leases,key=lambda x:(x['start'],x['id'])):
        key=(e['tenant'],e['product']);grant=grants.get(key);device=current.get((e['tenant'],e['device']))
        prior=[x for x in events if x['kind']=='contract' and (x['tenant'],x['product'])==key and x['at']<=e['start'] and x['status']=='active']
        historical=sorted(prior,key=lambda x:(x['version'],x['at']))[-1] if prior else None
        historical_seats=historical['seats'] if historical else 0
        finish=e['finish']
        for t in transfers:
            if t['tenant']==e['tenant'] and t['product']==e['product'] and t['from_device']==e['device'] and e['start']<=t['at']<finish:
                finish=min(finish,t['at'])
        conflict=bool(device and len(fingerprints[(e['tenant'],device['fingerprint'])])>1)
        active_at_start=sum(1 for x in allocated[key] if x['start']<=e['start']<x['finish'])
        same_device=any(x['device']==e['device'] and x['start']<finish and x['finish']>e['start'] for x in allocated[key])
        reason=('no_grant' if not grant or not grant['valid'] else 'unknown_device' if not device else
                'fingerprint_conflict' if conflict else 'outside_contract' if not(grant['event']['start']<=e['start']<finish<=grant['event']['finish']) else
                'duplicate_device_lease' if same_device else
                'seat_limit' if active_at_start>=historical_seats else 'active')
        item={'event':e,'device':e['device'],'finish':finish,'status':reason,'valid':reason=='active'}
        result[e['id']]=item
        if item['valid']:allocated[key].append({'start':e['start'],'finish':finish,'device':e['device']})
    return result,current
