"""Real same-render benchmark; each result is a separate immutable evaluation."""
import json
import uuid
from pathlib import Path
from .common import AI,atomic_json,canonical,utcnow
from .quality_service import QualityService

def benchmark(db,store,asset_id,model):
    roots=[j for j in db.jobs() if j['asset_id']==asset_id and j['status']=='Completed' and not json.loads(j['request_json']).get('quality_parent_job')]
    if not roots:raise ValueError('A completed baseline asset is required for benchmark')
    baseline=roots[0];ident=uuid.uuid4().hex;directory=store.output_root/'Benchmarks'/ident;directory.mkdir(parents=True)
    historical=[]
    for e in store.evaluations(asset_id):
        r=json.loads(e['report_json'] or '{}')
        if r.get('model') and r['model']!=model and r.get('overall_score') is not None:historical.append(r)
    trials=[]
    for strategy in ('six','split3'):
        print('Benchmark',strategy,flush=True)
        report=QualityService(db,store).evaluate(baseline['id'],{'quality':{'model':model,'image_strategy':strategy}})
        row=store.get(report['evaluation_id']);work=Path(row['work_dir']);telemetry=work/'gpu-telemetry.json'
        t=json.loads(telemetry.read_text('utf-8')) if telemetry.exists() else {}
        trial={'strategy':strategy,'evaluation_id':report['evaluation_id'],'job_id':baseline['id'],'status':'PASS' if report.get('scores') else 'FAIL',
               'decision':report['decision'],'scores':report.get('scores'),'overall_score':report.get('overall_score'),'error':report.get('error'),
               'performance':report.get('performance'),'model_placement':report.get('model_placement'),
               'peak_gpu_used_mib':max((s.get('used_mib',0) for s in t.get('samples',[])),default=None),
               'evaluation_dir':str(work),'cache_conditions':'Existing mesh render hashes verified; criteria source/parser cache; no resident Ollama model before trial. OS disk cache not flushed. split3 second request is warm.'}
        trials.append(trial);atomic_json(directory/('trial-'+strategy+'.json'),trial)
    passed=[t for t in trials if t['status']=='PASS'];best=min(passed,key=lambda t:t['performance']['total_seconds']) if passed else None
    result={'benchmark_id':ident,'asset_id':asset_id,'model':model,'baseline_job_id':baseline['id'],'trials':trials,
            'fastest_strategy':best['strategy'] if best else None,'status':'PASS' if len(passed)==2 else 'PARTIAL' if passed else 'FAIL',
            'historical_references':[{'evaluation_id':r['evaluation_id'],'model':r['model'],'score':r['overall_score'],'performance':r['performance']} for r in historical],
            'warning':'Different VLMs/protocols are not calibrated score equivalents. Historical Qwen total included extraction; inspect vision-only and total timings separately.'}
    store.db.execute('INSERT INTO QualityBenchmarks VALUES (?,?,?,?,?,?)',(ident,asset_id,model,result['status'],canonical(result),utcnow()));store.db.commit()
    atomic_json(directory/'benchmark.json',result);return result
