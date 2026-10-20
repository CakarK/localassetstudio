"""Queue real inference; persist full API history and never label failures as passes."""
import argparse
import json
import time
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
URL='http://127.0.0.1:8190'

def run(graph,label,timeout=3600):
    r=requests.post(URL+'/prompt',json={'prompt':graph,'client_id':'rei-local-test'},timeout=60)
    if not r.ok:raise RuntimeError(r.text)
    pid=r.json()['prompt_id']
    print('Queued',pid,label,flush=True)
    started=time.monotonic()
    while time.monotonic()-started<timeout:
        history=requests.get(URL+'/history/'+pid,timeout=60).json()
        if pid in history:
            result=history[pid]
            (ROOT/'Logs'/f'{label}.history.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
            print(json.dumps(result.get('status'),ensure_ascii=False),flush=True)
            if result.get('status',{}).get('status_str')!='success':raise RuntimeError('Inference failed; see '+label+'.history.json')
            return result
        time.sleep(3)
    raise TimeoutError('Job still running; inspect queue. No output has been deleted.')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('workflow');p.add_argument('--label',default='inference')
    args=p.parse_args()
    run(json.loads(Path(args.workflow).read_text('utf-8')),args.label)
