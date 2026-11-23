import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA='''
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS authorities(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, signer TEXT NOT NULL, start TEXT NOT NULL, finish TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contracts(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, product TEXT NOT NULL, version INTEGER NOT NULL, seats INTEGER NOT NULL, start TEXT NOT NULL, finish TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS features(id TEXT NOT NULL, feature TEXT NOT NULL, PRIMARY KEY(id,feature));
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, fingerprint TEXT NOT NULL, owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, device TEXT NOT NULL, tenant TEXT NOT NULL, product TEXT NOT NULL, start TEXT NOT NULL, finish TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, device TEXT NOT NULL, tenant TEXT NOT NULL, product TEXT NOT NULL, feature TEXT NOT NULL, start TEXT NOT NULL, finish TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlements(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, product TEXT NOT NULL, overage INTEGER NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS current_decisions(id TEXT PRIMARY KEY, result TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id TEXT NOT NULL, revision INTEGER NOT NULL, result TEXT NOT NULL, PRIMARY KEY(id,revision));
'''
TABLES=('authorities','contracts','features','devices','leases','sessions','settlements')
SUBSYSTEMS={'authority':'grant','contract':'grant','device':'activation','lease':'activation','transfer':'activation','session':'usage'}

def connect(path):
    if str(path)!=':memory:': Path(path).parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.executescript(SCHEMA);return c

def canonical(obj): return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(obj): return hashlib.sha256(canonical(obj).encode()).hexdigest()

def events(c): return [json.loads(r['body']) for r in c.execute('SELECT body FROM evidence ORDER BY at,id')]

def rebuild(c):
    from .logic import calculate
    for table in TABLES:c.execute(f'DELETE FROM {table}')
    source=events(c)
    decisions=calculate(source)
    for e in source:
        kind=e['kind']; id=e['id']
        if kind=='authority':
            c.execute('INSERT INTO authorities VALUES (?,?,?,?,?,?)',(id,e['tenant'],e['signer'],e['start'],e['finish'],e['status']))
        elif kind=='contract':
            c.execute('INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?)',(id,e['tenant'],e['product'],e['version'],e['seats'],e['start'],e['finish'],e['status']))
            for feature in e['features']:c.execute('INSERT INTO features VALUES (?,?)',(id,feature))
        elif kind=='device':c.execute('INSERT INTO devices VALUES (?,?,?,?)',(id,e['tenant'],e['fingerprint'],e['owner']))
        elif kind=='lease':c.execute('INSERT INTO leases VALUES (?,?,?,?,?,?,?)',(id,e['device'],e['tenant'],e['product'],e['start'],e['finish'],'active'))
        elif kind=='transfer':c.execute('INSERT INTO leases VALUES (?,?,?,?,?,?,?)',(id,e['from_device'],e['tenant'],e['product'],e['at'],e['at'],'transferred'))
        elif kind=='session':c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',(id,e['device'],e['tenant'],e['product'],e['feature'],e['start'],e['finish']))
    for key,item in decisions.items():
        if key.startswith('settlement:'):
            _,tenant,product=key.split(':',2)
            c.execute('INSERT INTO settlements VALUES (?,?,?,?,?,?)',(key,tenant,product,item['overage'],item['amount'],item['status']))
        body=canonical(item)
        old=c.execute('SELECT result,revision FROM current_decisions WHERE id=?',(key,)).fetchone()
        if old and old['result']==body:continue
        rev=(old['revision'] if old else 0)+1
        c.execute('INSERT INTO decision_history VALUES (?,?,?)',(key,rev,body))
        c.execute('INSERT INTO current_decisions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result,revision=excluded.revision',(key,body,rev))

def ingest(c,subsystem,items):
    if not isinstance(items,list) or not items:raise ValueError('nonempty event list required')
    inserted=replayed=0
    with c:
        for e in items:
            if not isinstance(e,dict) or SUBSYSTEMS.get(e.get('kind'))!=subsystem or not e.get('id') or not e.get('at'):raise ValueError('event identity or subsystem mismatch')
            sha=digest(e);old=c.execute('SELECT digest FROM evidence WHERE id=?',(e['id'],)).fetchone()
            if old:
                if old['digest']!=sha:raise ValueError('event id content collision')
                replayed+=1;continue
            c.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',(e['id'],subsystem,e['at'],canonical(e),sha));inserted+=1
        rebuild(c)
    return {'inserted':inserted,'replayed':replayed}

def report(c):
    return {'module':__file__,'evidence_count':c.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'decisions':{r['id']:json.loads(r['result']) for r in c.execute('SELECT * FROM current_decisions ORDER BY id')},
            'history':[dict(r) for r in c.execute('SELECT * FROM decision_history ORDER BY id,revision')]}

def audit(c):
    from .logic import calculate
    source=events(c)
    expected=calculate(source)
    persisted=report(c)['decisions']
    changed=sorted(k for k in set(expected)|set(persisted) if expected.get(k)!=persisted.get(k))
    replay=connect(':memory:')
    try:
        with replay:
            for e in source:
                replay.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',
                    (e['id'],SUBSYSTEMS[e['kind']],e['at'],canonical(e),digest(e)))
            rebuild(replay)
        for table in TABLES:
            current=[tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY 1')]
            recomputed=[tuple(row) for row in replay.execute(f'SELECT * FROM {table} ORDER BY 1')]
            if current!=recomputed:changed.append(f'table:{table}')
    finally:replay.close()
    return {'ok':not changed,'evidence_count':len(source),'changed':changed}
