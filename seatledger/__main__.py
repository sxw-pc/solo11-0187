import argparse
import json
import sys
from pathlib import Path
from .store import connect,ingest,report,audit

COMMANDS={'grant-import':'grant','activation-import':'activation','usage-import':'usage'}
ROOT=Path(__file__).resolve().parent.parent

def main(argv=None):
    p=argparse.ArgumentParser(prog='seatledger');sub=p.add_subparsers(dest='command',required=True)
    for name in [*COMMANDS,'demo','report','audit','init']:
        q=sub.add_parser(name);q.add_argument('--db',default='seatledger.sqlite')
        if name in COMMANDS:q.add_argument('--file',required=True)
    a=p.parse_args(argv)
    try:
        c=connect(a.db)
        if a.command in COMMANDS:
            value=ingest(c,COMMANDS[a.command],json.loads(Path(a.file).read_text(encoding='utf-8')))
        elif a.command=='demo':
            steps=[]
            for subsystem,name in [('grant','grant-ok.json'),('activation','activation-ok.json'),('usage','usage-ok.json'),
                                   ('grant','grant-bad.json'),('activation','activation-bad.json'),('usage','usage-bad.json')]:
                result=ingest(c,subsystem,json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8')))
                steps.append({'fixture':name,**result,'decision_count':len(report(c)['decisions'])})
            value={'module':str(Path(__file__).resolve()),'steps':steps,'report':report(c),'audit':audit(c)}
        elif a.command=='report':value=report(c)
        elif a.command=='audit':value=audit(c)
        else:value={'initialized':True,'module':str(Path(__file__).resolve())}
        print(json.dumps(value,ensure_ascii=False,indent=2))
        return 0 if a.command!='audit' or value['ok'] else 2
    except (ValueError,OSError,KeyError,TypeError) as e:
        print(f'error: {e}',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
