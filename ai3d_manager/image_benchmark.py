"""Durable cross-provider experiment with bounded references and two Shapes."""
import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .common import AI, ProcessLock, ProductionError, atomic_json, canonical, digest, intact, utcnow
from .quality_store import QualityStore
from .image_providers import ImageGenerationProvider, load_registry, gpu_preflight,DualComfyQualityLease
from .reference_prompts import compile_prompt
from .reference_fidelity import ReferenceFidelityEvaluator, current_fidelity
from .quality_gates import QualityGateService, current_gate, outputs_for
from .controlled_generation import claim_budget
from .quality_improve import ProductionTelemetry
from .quality_config import load_quality
from .quality_vlm import OllamaVLM
from .worker import Worker, settings_for

def category_assets(db):
    result={}
    for asset in db.assets():
        category=asset.get('category_profile')
        if category in ('Buildings','Towers','Organic','Debris'):result.setdefault(category,asset['asset_id'])
    return result
PROMPT_VERSION = 'cross-provider-reference-3.4.0'


def adapt_prompt(asset, provider, profile='controlled'):
    if profile not in ('controlled', 'model-optimized'): raise ValueError('Unknown prompt profile')
    base = compile_prompt(asset)
    if profile == 'controlled': positive = base['generation_prompt']
    else:
        # Formatting only; same literal generation requirements and style.
        positive = ('Single isolated game asset. Required subject and materials:\n'+base['generation_prompt']+
                    '\nComposition: complete object in a 45-degree elevated view, empty margins, plain neutral background, no terrain or presentation plinth.')
    return {'version': PROMPT_VERSION, 'profile': profile, 'provider_id': provider['id'], 'positive': positive,
            'style': asset['shared_style'], 'negative': base['negative'], 'negative_supported': provider['negative_prompt'],
            'source_sha256': base['source_sha256'], 'source_requirements': base['preserved_source_phrases'],
            'adaptation': 'Unchanged controlled meaning' if profile == 'controlled' else 'Natural-language labeled composition; no new/removed source requirements'}


def sidecar(store): return {'db': str(store.path), 'root': str(store.output_root)}
def get_plan(store, ident): return json.loads(store.db.execute('SELECT config_json FROM ImageBenchmarks WHERE id=?', (ident,)).fetchone()[0])
def ref_rows(store, bench, asset=None):
    return [dict(r) for r in store.db.execute('SELECT * FROM ImageReferences WHERE benchmark_id=?'+(' AND asset_id=?' if asset else '')+' ORDER BY rowid', (bench, asset) if asset else (bench,))]


def budget_boundary(db, store, plan):
    rows = ref_rows(store, plan['id']) + [dict(r) for r in store.db.execute('SELECT job_id FROM ImageShapeRuns WHERE benchmark_id=?', (plan['id'],))]
    size = 0; elapsed = 0.
    for r in rows:
        if not r.get('job_id'): continue
        job = db.job(r['job_id']); size += sum(p.stat().st_size for p in Path(job['output_dir']).rglob('*') if p.is_file())
        for s in db.stages(job['id']):
            if s['started_at']:
                end = datetime.fromisoformat(s['ended_at']) if s['ended_at'] else datetime.now(timezone.utc)
                elapsed += max(0, (end-datetime.fromisoformat(s['started_at'])).total_seconds())
        for table in ['QualityGateReports', 'ReferenceFidelityReports']:
            for raw in store.db.execute('SELECT report_json FROM '+table+' WHERE job_id=?', (job['id'],)): elapsed += json.loads(raw[0]).get('elapsed_seconds', 0)
        for raw in store.db.execute('SELECT report_json FROM QualityEvaluations WHERE job_id=? AND report_json IS NOT NULL',(job['id'],)):
            elapsed+=json.loads(raw[0]).get('performance',{}).get('total_seconds',0)
    if size > plan['disk_budget_bytes']: raise ProductionError('DiskBudget', 'Campaign output disk budget reached; no new stage')
    if shutil.disk_usage(db.output_root.parent).free < plan['minimum_free_disk_bytes']: raise ProductionError('DiskBudget', 'Insufficient disk headroom')
    if elapsed > plan['execution_budget_seconds']: raise ProductionError('GPUTimeBudget', 'Conservative execution budget reached at stage boundary')
    return {'output_bytes': size, 'execution_seconds': elapsed, 'soft_stage_boundary_only': True}


def require_fidelity(db, asset):
    addr = asset['quality_sidecar']; store = QualityStore(addr['db'], addr['root'])
    try:
        selected = store.db.execute('SELECT * FROM ImageReferences WHERE id=?', (asset['image_reference_id'],)).fetchone()
        if not selected: raise ProductionError('ReferenceFidelityBlocked', 'Selected reference record missing')
        gate = current_gate(db, store, selected['job_id'], 'reference'); fidelity = current_fidelity(db, store, selected['job_id'])
        if not gate or gate['status'] != 'PASS' or not fidelity or fidelity['status'] != 'PASS': raise ProductionError('ReferenceFidelityBlocked', 'Current Reference Gate and critical fidelity PASS required')
        outputs = outputs_for(db, selected['job_id'])
        metadata = json.loads(asset.get('selected_reference_sources_json', '{}'))
        if any(outputs[s][k] != metadata.get(k) or not intact(outputs[s][k]) for s, k in [('ReferenceGenerated', 'reference'), ('ReferenceProcessed', 'preprocessed')]): raise ProductionError('ReferenceIntegrity', 'Selected original/mask changed')
        return selected, gate, fidelity
    finally: store.close()


def guard_submission(db, asset, attempt, operation, provider=None):
    addr = asset['quality_sidecar']; store = QualityStore(addr['db'], addr['root'])
    try:
        plan = get_plan(store, asset['phase34_benchmark_id']); budget_boundary(db, store, plan)
        if load_quality() != plan['vlm_profile']: raise ProductionError('VLMConfigurationMismatch', 'Benchmark VLM profile changed')
        if operation == 'reference':
            slot = store.db.execute('SELECT rowid,* FROM ImageReferences WHERE id=? AND benchmark_id=?', (asset['image_reference_id'], plan['id'])).fetchone()
            if not slot or slot['job_id'] != attempt['job_id'] or slot['provider_id'] not in plan['providers']: raise ProductionError('ProductionBudgetMismatch', 'Reference ownership mismatch')
            if provider != plan['providers'][slot['provider_id']]: raise ProductionError('ProviderConfigurationMismatch', 'Provider snapshot changed')
            refs = ref_rows(store, plan['id'])
            if len(refs) > 22 or len({r['provider_id'] for r in refs}) > 4: raise ProductionError('ProductionBudgetExhausted', 'Reference/provider cap exceeded')
            same = [r for r in refs if (r['asset_id'], r['provider_id'], r['profile']) == (slot['asset_id'], slot['provider_id'], slot['profile'])]
            maximum = 2 if slot['asset_id'] == plan['primary_asset_id'] and slot['profile'] == 'controlled' else 1
            if len(same) > maximum or sum(r['profile'] == 'model-optimized' for r in refs) > 2: raise ProductionError('ProductionBudgetExhausted', 'Seed/profile budget exceeded')
            number = slot['rowid']; endpoint = provider['comfy_url']; minimum = provider['minimum_free_mib']
        else:
            runs = [dict(r) for r in store.db.execute('SELECT rowid,* FROM ImageShapeRuns WHERE benchmark_id=? ORDER BY rowid', (plan['id'],))]
            run = next((r for r in runs if r['job_id'] == attempt['job_id']), None)
            if not run or len(runs) > 2: raise ProductionError('ProductionBudgetExhausted', 'Only two owned Shape/PBR runs allowed')
            require_fidelity(db, asset)
            gate = current_gate(db, store, attempt['job_id'], 'reference' if operation == 'shape' else 'geometry')
            if not gate or gate['status'] != 'PASS': raise ProductionError('ProductionGateBlocked', 'Current downstream gate PASS required')
            number = run['rowid']; endpoint = 'http://127.0.0.1:8190'; minimum = 12000
        if attempt['state'] not in ('Submitting', 'Submitted', 'SubmissionUnknown', 'NeedsReview'):
            sample = gpu_preflight(endpoint, minimum)
            atomic_json(Path(attempt['work_dir'])/'phase34-gpu-preflight.json', sample)
        claim_budget(store, plan['id'], operation, number, attempt['id'])
    finally: store.close()


def rank_key(row):
    m = row.get('fidelity_metrics') or {}; mask = row.get('mask_metrics') or {}
    return (row.get('gate_status') == 'PASS' and row.get('fidelity_status') == 'PASS',
            -len(m.get('critical_unmet', [])), -len(m.get('major_unmet', [])),
            m.get('requirement_fidelity') or 0, m.get('style_consistency') or 0,
            m.get('silhouette_quality') or 0, mask.get('largest_component_fraction', 0), -mask.get('edge_contact_fraction', 1))


def choose_profile_reference(controlled,optimized):
    alternative=next((r for r in optimized if r['provider_id']==controlled['provider_id'] and r['eligible_for_shape']),None)
    return alternative if alternative and (not controlled['eligible_for_shape'] or rank_key(alternative)>rank_key(controlled)) else controlled


def compare_references(db, store, asset_id, plan=None):
    if plan is None:
        row = store.db.execute('SELECT config_json FROM ImageBenchmarks ORDER BY created_at DESC LIMIT 1').fetchone()
        if not row: return {'asset_id': asset_id, 'profiles': {}, 'status': 'NotBenchmarked'}
        plan = json.loads(row[0])
    groups = {}
    for r in ref_rows(store, plan['id'], asset_id):
        outputs = outputs_for(db, r['job_id']); gate = current_gate(db, store, r['job_id'], 'reference'); fidelity = current_fidelity(db, store, r['job_id'])
        meta = json.loads(r['metadata_json']); p = plan['providers'][r['provider_id']]
        item = {'reference_id': r['id'], 'job_id': r['job_id'], 'provider_id': r['provider_id'], 'model': p['name'], 'revision': p['revision'], 'quantization': p['quantization'],
                'seed': r['seed'], 'profile': r['profile'], 'workflow': p['workflow_id'], 'resolution': [p['width'], p['height']],
                'gate_status': gate['status'] if gate else 'NeedsReview', 'fidelity_status': fidelity['status'] if fidelity else 'NeedsReview',
                'fidelity_metrics': fidelity.get('metrics') if fidelity else None, 'mask_metrics': gate.get('deterministic', {}).get('metrics') if gate else None,
                'fidelity_protocol': digest({'version': fidelity['version'], 'requirements': fidelity['requirements_sha256'], 'profile': fidelity['vlm_profile']}) if fidelity else None,
                'artifacts': {k: outputs.get(s, {}).get(k) for s, k in [('ReferenceGenerated', 'reference'), ('ReferenceProcessed', 'preprocessed')]}, 'metadata': meta}
        item['eligible_for_shape'] = bool(gate and gate['status'] == 'PASS' and fidelity and fidelity['status'] == 'PASS' and all(i and intact(i) for i in item['artifacts'].values()))
        groups.setdefault(r['profile'], []).append(item)
    for profile, items in groups.items():
        items.sort(key=rank_key, reverse=True)
    return {'benchmark_id': plan['id'], 'asset_id': asset_id, 'profiles': groups, 'profiles_compared_separately': True,
            'automatic_default_changed': False, 'confidence_is_probability': False, 'status': 'Evaluated'}


class ImageBenchmarkService:
    def __init__(self, db, store, worker_factory=Worker, gate_factory=QualityGateService, fidelity_factory=ReferenceFidelityEvaluator, model_check=None, monitor_gpu=True):
        self.db=db; self.store=store; self.worker_factory=worker_factory; self.gate_factory=(lambda d,s:QualityGateService(d,s,lease_factory=DualComfyQualityLease)) if gate_factory is QualityGateService else gate_factory; self.fidelity_factory=fidelity_factory; self.model_check=model_check; self.monitor_gpu=monitor_gpu

    def worker(self):
        return self.worker_factory(self.db,allow_gpu=True,gate_service_factory=self.gate_factory) if self.worker_factory is Worker else self.worker_factory(self.db,allow_gpu=True)

    def prepare(self, providers=None):
        registry = load_registry(); ids = providers or registry['benchmark_providers']
        if not 1 <= len(ids) <= 4 or len(set(ids)) != len(ids): raise ValueError('One to four distinct providers required')
        if any(i not in registry['providers'] or registry['providers'][i].get('research_only') for i in ids): raise ValueError('Research-only/unknown provider cannot benchmark')
        assets=category_assets(self.db)
        if 'Buildings' not in assets:raise ValueError('Benchmark requires a user asset with category_profile Buildings')
        primary=assets['Buildings']
        config = {'primary_asset_id':primary,'category_assets':assets,'providers': {i: registry['providers'][i] for i in ids}, 'vlm_profile': load_quality(), 'source_catalog_sha256': self.db.asset(primary)['source']['sha256'] if 'source' in self.db.asset(primary) else digest({a['asset_id']:a['prompt'] for a in self.db.assets()}),
                  'prompt_version': PROMPT_VERSION, 'disk_budget_bytes': 3*1024**3, 'minimum_free_disk_bytes': 3*1024**3, 'execution_budget_seconds': 14400,
                  'budget': {'providers': 4, 'primary_seeds_per_provider': 2, 'optimized_references': 2, 'other_categories_per_provider': 1, 'shape': 2, 'pbr': 2, 'final_evaluations': 2}}
        key = digest(config); previous = self.store.db.execute('SELECT * FROM ImageBenchmarks ORDER BY created_at LIMIT 1').fetchone()
        if previous:
            if previous['request_key'] != key: raise ProductionError('ImageBenchmarkBudgetFixed', 'Campaign providers/config/source already fixed; no extra production')
            return json.loads(previous['config_json'])
        config.update(id=uuid.uuid4().hex, created_at=utcnow())
        self.store.db.execute('INSERT INTO ImageBenchmarks VALUES (?,?,?,?)', (config['id'], key, canonical(config), config['created_at'])); self.store.db.commit()
        atomic_json(self.store.output_root/'ImageBenchmarks'/config['id']/'benchmark.json', config)
        return config

    def run(self, asset_id, providers=None, profile='controlled', provider_subset=None):
        if asset_id not in category_assets(self.db).values(): raise ValueError('Only exact approved catalog asset IDs are supported: '+str(list(category_assets(self.db).values())))
        with ProcessLock(self.store.path.with_suffix('.image-benchmark.lock')):
            plan = self.prepare(providers); self.model_check() if self.model_check else OllamaVLM().metadata()
            selected = provider_subset or list(plan['providers'])
            if any(i not in plan['providers'] for i in selected): raise ValueError('Provider outside fixed campaign')
            if profile == 'model-optimized' and (asset_id != plan['primary_asset_id'] or len(selected) > 2): raise ValueError('Only two extra optimized primary-asset references allowed')
            seeds = [1051, 2060] if asset_id == plan['primary_asset_id'] and profile == 'controlled' else [3069 if profile == 'model-optimized' else 1051]
            asset = {k:v for k,v in self.db.asset(asset_id).items() if k not in ('jobs','status')}
            for pid in selected:
                provider = ImageGenerationProvider(pid)
                verification = provider.verify()
                if verification['status'] != 'PASS':
                    atomic_json(self.store.output_root/'ImageBenchmarks'/plan['id']/('blocked-'+pid+'.json'), verification); continue
                for seed in seeds:
                    old = self.store.db.execute('SELECT * FROM ImageReferences WHERE benchmark_id=? AND asset_id=? AND provider_id=? AND profile=? AND seed=?', (plan['id'], asset_id, pid, profile, seed)).fetchone()
                    if old: row = dict(old)
                    else:
                        if profile == 'model-optimized' and self.store.db.execute("SELECT COUNT(*) FROM ImageReferences WHERE benchmark_id=? AND profile='model-optimized'", (plan['id'],)).fetchone()[0] >= 2: raise ProductionError('ProductionBudgetExhausted','Two optimized references already planned')
                        row = {'id':uuid.uuid4().hex, 'benchmark_id':plan['id'], 'asset_id':asset_id, 'provider_id':pid, 'profile':profile, 'seed':seed, 'job_id':None, 'gate_report_id':None, 'fidelity_report_id':None, 'metadata_json':'{}'}
                        self.store.db.execute('INSERT INTO ImageReferences VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(row.values())); self.store.db.commit()
                    prompt = adapt_prompt(asset, provider.spec, profile)
                    request = {**asset, 'image_provider':pid, 'production_role':'reference_candidate', 'phase34_benchmark_id':plan['id'], 'image_reference_id':row['id'], 'quality_sidecar':sidecar(self.store)}
                    settings = settings_for(asset, {'seed':seed,'image_provider':pid,'image_profile':profile,'prompt':prompt['positive'],'style':prompt['style'],'negative':prompt['negative'],'max_transient_retries':0,'allow_quality_reduction':False})
                    if not row['job_id']:
                        job,_ = self.db.enqueue(request, 'phase34_ref_'+pid.replace('-','_'), settings); row['job_id']=job['id']
                        self.store.db.execute('UPDATE ImageReferences SET job_id=? WHERE id=?',(job['id'],row['id'])); self.store.db.commit()
                    job = self.db.job(row['job_id']); outputs = outputs_for(self.db, job['id'])
                    if job['status'] == 'ReferenceReady':
                        if not all(intact(i) for s in outputs.values() for i in s.values()): raise ProductionError('ReferenceIntegrity','Completed reference changed; no regeneration')
                    elif job['status'] not in ('Failed','NeedsReview','Cancelled'):
                        budget_boundary(self.db,self.store,plan)
                        from contextlib import nullcontext
                        telemetry = self.store.output_root/'ImageBenchmarks'/plan['id']/(row['id']+'-gpu.json')
                        with ProductionTelemetry(telemetry) if self.monitor_gpu else nullcontext(): self.worker().run(job['id'])
                    if self.db.job(job['id'])['status'] != 'ReferenceReady': continue
                    print('Reference benchmark', asset_id,pid,seed,'evaluating existing gates and fidelity',flush=True)
                    budget_boundary(self.db,self.store,plan)
                    gate=self.gate_factory(self.db,self.store).run('reference',job['id'])
                    budget_boundary(self.db,self.store,plan)
                    fidelity=self.fidelity_factory(self.db,self.store).evaluate(job['id'])
                    outputs=outputs_for(self.db,job['id']); inference=outputs['ReferenceGenerated'].get('image_inference')
                    perf=json.loads(Path(inference['path']).read_text('utf-8')) if inference else {}
                    telemetry=self.store.output_root/'ImageBenchmarks'/plan['id']/(row['id']+'-gpu.json')
                    gpu=json.loads(telemetry.read_text('utf-8')) if telemetry.exists() else {}
                    meta={'prompt':prompt,'sources':{k:outputs[s][k] for s,k in [('ReferenceGenerated','reference'),('ReferenceProcessed','preprocessed')]},'provider':provider.spec,
                          'generation_seconds':perf.get('elapsed_seconds'),'gpu_peak_used_mib':gpu.get('peak_gpu_used_mib'),
                          'output_disk_bytes':sum(p.stat().st_size for p in Path(job['output_dir']).rglob('*') if p.is_file()),'model_disk_bytes':sum(f['size'] for f in provider.spec['files'])}
                    self.store.db.execute('UPDATE ImageReferences SET gate_report_id=?,fidelity_report_id=?,metadata_json=? WHERE id=?',(gate['report_id'],fidelity['report_id'],canonical(meta),row['id']));self.store.db.commit()
            report=compare_references(self.db,self.store,asset_id,plan);atomic_json(self.store.output_root/'ImageBenchmarks'/plan['id']/(asset_id+'-comparison.json'),report);return report

    def optimized(self):
        plan=self.prepare(); report=compare_references(self.db,self.store,plan['primary_asset_id'],plan)
        best=[]
        for item in report['profiles'].get('controlled',[]):
            if item['provider_id'] not in best: best.append(item['provider_id'])
        return self.run(plan['primary_asset_id'],profile='model-optimized',provider_subset=best[:2]) if best else report

    def shapes(self):
        with ProcessLock(self.store.path.with_suffix('.image-benchmark.lock')):
            plan=self.prepare(); report=compare_references(self.db,self.store,plan['primary_asset_id'],plan)
            # Profiles are ranked independently. Optional optimized result replaces
            # only that same provider's controlled candidate when it is eligible.
            pool=report['profiles'].get('controlled',[]); optimized=report['profiles'].get('model-optimized',[])
            selected=[]
            for item in sorted(pool,key=rank_key,reverse=True):
                if item['provider_id'] in {r['provider_id'] for r in selected}:continue
                choice=choose_profile_reference(item,optimized)
                if choice['eligible_for_shape']:selected.append(choice)
                if len(selected)==2:break
            results=[]
            for choice in selected:
                results.append(self._shape(plan,choice))
            result={'benchmark_id':plan['id'],'selected_references':[r['reference_id'] for r in selected],'different_providers':len({r['provider_id'] for r in selected}),'runs':results,'two_provider_comparison_available':len(selected)==2,'status':'Evaluated' if len(selected)==2 else 'BLOCKED','reason':None if len(selected)==2 else 'Fewer than two different providers have current Reference Gate PASS and critical fidelity PASS; missing comparisons were not fabricated'}
            atomic_json(self.store.output_root/'ImageBenchmarks'/plan['id']/'hunyuan-comparison.json',result);return result

    def _shape(self,plan,choice):
        previous=self.store.db.execute('SELECT * FROM ImageShapeRuns WHERE reference_id=?',(choice['reference_id'],)).fetchone()
        if previous: run=dict(previous); metadata=json.loads(run['metadata_json'])
        else:
            rows=self.store.db.execute('SELECT * FROM ImageShapeRuns WHERE benchmark_id=?',(plan['id'],)).fetchall()
            if len(rows)>=2:raise ProductionError('ProductionBudgetExhausted','Two Shape slots already owned')
            if any(json.loads(r['metadata_json'])['provider_id']==choice['provider_id'] for r in rows):raise ProductionError('ProductionBudgetExhausted','Each Shape must use a different provider')
            ident=uuid.uuid4().hex; refjob=self.db.job(choice['job_id']); asset=json.loads(refjob['request_json']); sources=choice['artifacts']
            asset.update(production_role='controlled_asset',image_reference_id=choice['reference_id'],image_profile=choice['profile'],selected_reference_sources_json=canonical(sources))
            settings=settings_for(asset,{'seed':9043,'shape_steps':40,'shape_resolution':384,'polygon_target':asset['polygon_target'],'texture_size':asset['texture_size'],'max_transient_retries':0,'allow_quality_reduction':False,'image_profile':choice['profile']})
            settings['reuse_files']={s:{k:sources[k]} for s,k in [('ReferenceGenerated','reference'),('ReferenceProcessed','preprocessed')]}
            job,_=self.db.enqueue(asset,'phase34_shape_'+choice['provider_id'].replace('-','_'),settings)
            metadata={'reference_job_id':choice['job_id'],'provider_id':choice['provider_id'],'reference':choice,'baseline_job_id':None,'baseline_evaluation_id':None}
            run={'id':ident,'benchmark_id':plan['id'],'reference_id':choice['reference_id'],'job_id':job['id'],'evaluation_id':None,'status':'Prepared','metadata_json':canonical(metadata)}
            self.store.db.execute('INSERT INTO ImageShapeRuns VALUES (?,?,?,?,?,?,?)',tuple(run.values()));self.store.db.commit()
        job=self.db.job(run['job_id'])
        if job['status']=='Completed':
            if not all(intact(i) for s in outputs_for(self.db,job['id']).values() for i in s.values()):raise ProductionError('ArtifactIntegrity','Completed delivery changed; no repeat Shape')
        elif job['status'] not in ('Failed','NeedsReview','Cancelled'):
            from contextlib import nullcontext
            with ProductionTelemetry(self.store.output_root/'ImageBenchmarks'/plan['id']/(run['id']+'-production-gpu.json')) if self.monitor_gpu else nullcontext():job=self.worker().run(job['id'])
        if job['status']=='Completed' and not run['evaluation_id']:
            budget_boundary(self.db,self.store,plan)
            claim_budget(self.store,plan['id'],'final_evaluation',int(self.store.db.execute('SELECT rowid FROM ImageShapeRuns WHERE id=?',(run['id'],)).fetchone()[0]),run['id'])
            ident=digest({'phase34_run':run['id'],'operation':'final'})[:32];old=self.store.db.execute('SELECT id FROM QualityEvaluations WHERE id=?',(ident,)).fetchone();evaluation=self.store.begin(job,{},ident=ident)
            run['evaluation_id']=ident;self.store.db.execute("UPDATE ImageShapeRuns SET evaluation_id=?,status='Evaluating' WHERE id=?",(ident,run['id']));self.store.db.commit()
            if not old:
                from .quality_service import QualityService
                QualityService(self.db,self.store,lease_factory=DualComfyQualityLease).evaluate(job['id'],prepared_evaluation=evaluation)
        run['status']=job['status'];self.store.db.execute('UPDATE ImageShapeRuns SET status=? WHERE id=?',(run['status'],run['id']));self.store.db.commit()
        trace={**run,'metadata':metadata,'checkpoints':[{'stage':s['name'],'inputs':json.loads(s['inputs_json']),'outputs':self.db.artifact_dict(s['id'])} for s in self.db.stages(job['id']) if s['status']=='Completed'],
               'reference_stages':[{'stage':s['name'],'inputs':json.loads(s['inputs_json']),'outputs':self.db.artifact_dict(s['id'])} for s in self.db.stages(metadata['reference_job_id']) if s['status']=='Completed'],
               'final_visual_report':json.loads(self.store.get(run['evaluation_id'])['report_json'] or '{}') if run['evaluation_id'] else None,
               'reference_gate':current_gate(self.db,self.store,metadata['reference_job_id'],'reference'),'reference_fidelity':current_fidelity(self.db,self.store,metadata['reference_job_id']),
               'geometry_gate':current_gate(self.db,self.store,job['id'],'geometry'),'manual_approval':False}
        atomic_json(self.store.output_root/'ImageBenchmarks'/plan['id']/(run['id']+'-traceability.json'),trace);return trace


def recommend(db,store,category):
    selected=category_assets(db)
    if category not in selected:raise ValueError('Exact benchmark category required: '+str(list(selected)))
    report=compare_references(db,store,selected[category]);items=report.get('profiles',{}).get('controlled',[])
    best=next((r for r in items if r['eligible_for_shape']),None)
    result={'category':category,'asset_id':selected[category],'recommended_provider':best['provider_id'] if best else None,'evidence_reference_id':best['reference_id'] if best else None,'sample_count':len(items),'sufficient_for_default_change':False,'automatic_default_changed':False,'reason':'Exploratory evidence from one catalog asset in this category; no calibrated category winner'}
    atomic_json(store.output_root/'ImageRecommendations'/(category+'-'+digest(result)[:16]+'.json'),result);return result


def model_comparison(db,store,asset_id):
    rows=[dict(r) for r in store.db.execute('SELECT * FROM ImageShapeRuns') if db.job(r['job_id'])['asset_id']==asset_id]
    baseline_row=store.db.execute('SELECT report_json FROM QualityEvaluations WHERE asset_id=? AND report_json IS NOT NULL ORDER BY created_at LIMIT 1',(asset_id,)).fetchone()
    baseline=json.loads(baseline_row[0]) if baseline_row and baseline_row[0] else None
    items=[]
    for row in rows:
        evaluation=json.loads(store.get(row['evaluation_id'])['report_json'] or '{}') if row['evaluation_id'] else {}
        items.append({'job_id':row['job_id'],'provider_id':json.loads(row['metadata_json'])['provider_id'],'status':row['status'],'reference_id':row['reference_id'],
                      'reference_gate':current_gate(db,store,row['job_id'],'reference'),'geometry_gate':current_gate(db,store,row['job_id'],'geometry'),
                      'evaluation':evaluation or None,'technical_outputs':outputs_for(db,row['job_id']),
                      'same_baseline_protocol':bool(evaluation and baseline and all(evaluation.get(k)==baseline.get(k) for k in ('model_version','protocol_sha256','interpretation_sha256','policy'))),
                      'manual_approval':False})
    complete=[i for i in items if i['geometry_gate'] and i['geometry_gate']['status']=='PASS'
              and i['evaluation'] and i['evaluation'].get('overall_score') is not None
              and len(i['evaluation'].get('render_artifacts',[]))==6 and i['same_baseline_protocol']]
    available=len(items)==2 and len(complete)==2
    result={'asset_id':asset_id,'baseline':baseline,'candidates':items,'automatic_winner':None,
            'completed_evaluations':len(complete),'status':'Compared' if available else 'BLOCKED',
            'reason':None if available else 'Two complete same-protocol 3D evaluations unavailable; recorded partial runs remain visible and no missing result is invented'}
    atomic_json(store.output_root/'ImageComparisons'/(asset_id+'-model-comparison.json'),result);return result
