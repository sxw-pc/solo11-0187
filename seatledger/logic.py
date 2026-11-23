from . import grants,activation,usage

def calculate(events):
    grants_by_key=grants.decide(events)
    leases,devices=activation.decide(events,grants_by_key)
    sessions,settlements=usage.decide(events,grants_by_key,leases)
    out={}
    for (tenant,product),g in grants_by_key.items():
        out[f'grant:{tenant}:{product}']={'status':g['reason'],'seats':g['seats'],'features':g['features'],'version':g['event']['version'],'conflict':g['conflict']}
    for (tenant,device),d in devices.items():
        out[f'device:{tenant}:{device}']={'fingerprint':d['fingerprint'],'owner':d['owner'],'version_at':d['at']}
    for id,l in leases.items():out[f'lease:{id}']={'status':l['status'],'device':l['device'],'finish':l['finish']}
    for id,s in sessions.items():out[f'session:{id}']=s
    out.update(settlements)
    return out
