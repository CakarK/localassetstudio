"""At most two source-preserving candidates; restart-safe job IDs and budgets."""
import json
import math
import threading
import time
import uuid
from pathlib import Path
from .common import AI,ProcessLock,ProductionError,atomic_json,canonical,digest,utcnow,intact
from .prompt_refinement import refine
from .quality_planner import BudgetedWorker,directory_size
from .quality_service import QualityService
from .quality_vlm import gpu_sample
from .quality_compact import PROMPT_VERSION

def select_stage(report):
    categories={f['category'] for f in report.get('findings',[])}
    if categories & {'prompt_fidelity','art_style','top_down_readability'}:return 'ReferenceGenerated'
    if 'geometry' in categories:return 'GeometryGenerated'
    if 'texture' in categories:return 'PBRGenerated'
    if 'blender' in categories:return 'BlenderExported'
    return 'ReferenceGenerated'

def reuse_for_stage(outputs,stage):
    keep={'ReferenceGenerated':[], 'GeometryGenerated':['ReferenceGenerated','ReferenceProcessed'],
          'PBRGenerated':['ReferenceGenerated','ReferenceProcessed','GeometryGenerated','GeometryOptimized'],
          'BlenderExported':['ReferenceGenerated','ReferenceProcessed','GeometryGenerated','GeometryOptimized','PBRGenerated','Scaled']}[stage]
    return {s:{k:v for k,v in outputs[s].items() if k!='origin'} for s in keep}

def used_production_seconds(db,jobs):
    # Count all owned GPU stage attempts, including failed attempts, across restarts.
    from datetime import datetime,timezone
    total=0
    for job in jobs:
        for s in db.stages(job):
            if s['name'] not in ('ReferenceGenerated','GeometryGenerated','PBRGenerated'):continue
            end=datetime.fromisoformat(s['ended_at']) if s['ended_at'] else datetime.now(timezone.utc)
            total+=max(0,(end-datetime.fromisoformat(s['started_at'])).total_seconds())
    return total

def prepare_improvement(db,store,parent_id,candidates=2,gpu_seconds=900,disk_mb=2048):
    if type(candidates) is not int or not 1<=candidates<=2:raise ValueError('Phase 3.1 permits at most two new candidates')
    if any(not math.isfinite(x) or x<=0 for x in (gpu_seconds,disk_mb)):raise ValueError('Finite positive budgets required')
    parent=db.job(parent_id);asset=json.loads(parent['request_json'])
    prior=store.db.execute('SELECT * FROM QualityImprovements WHERE parent_job_id=? ORDER BY created_at LIMIT 1',(parent_id,)).fetchone()
    if prior:
        plan=json.loads(prior['plan_json'])
        if len(plan['slots'])!=candidates:raise ValueError('Existing improvement already owns its slots; no extra candidates created')
        return plan
    rows=store.db.execute("SELECT report_json FROM QualityBenchmarks WHERE asset_id=? AND status IN ('PASS','PARTIAL') ORDER BY created_at DESC",(asset['asset_id'],)).fetchall()
    benchmark=next((json.loads(r[0]) for r in rows if json.loads(r[0]).get('fastest_strategy')),None)
    if not benchmark:raise ProductionError('BenchmarkRequired','Complete a successful Gemma benchmark before improvement production')
    refinement=refine(db,store,parent_id);settings=json.loads(parent['settings_json'])
    report=store.report(parent_id);stage=select_stage(report)
    supported=[a['target_stage'] for a in refinement['planner']['actions'] if a['automatic_execution_supported']]
    order=['ReferenceGenerated','GeometryGenerated','PBRGenerated','BlenderExported']
    if supported:stage=min(supported,key=order.index)
    ident=uuid.uuid4().hex;config={'candidates':candidates,'gpu_seconds':gpu_seconds,'disk_mb':disk_mb,
                                  'quality':{'model':benchmark['model'],'image_strategy':benchmark['fastest_strategy']}}
    key=digest({'parent':parent_id,'phase':'3.1','refinement':refinement['request_key']})
    slots=[{'slot':chr(65+n),'seed':settings['seed']+(n+1)*1001,'job_id':None,'evaluation_id':None} for n in range(candidates)]
    plan={'improvement_id':ident,'request_key':key,'parent_job_id':parent_id,'asset_id':asset['asset_id'],'config':config,
          'refinement':refinement,'target_stage':stage,'baseline_evaluation_id':next(t['evaluation_id'] for t in benchmark['trials'] if t['strategy']==benchmark['fastest_strategy']),
          'slots':slots,'created_at':utcnow()}
    store.db.execute('INSERT INTO QualityImprovements VALUES (?,?,?,?,?,?,?)',(ident,key,parent_id,'Prepared',canonical(config),canonical(plan),utcnow()));store.db.commit()
    save_improvement(store,plan,'Prepared');return plan

def save_improvement(store,plan,status):
    store.db.execute('UPDATE QualityImprovements SET status=?,plan_json=? WHERE id=?',(status,canonical(plan),plan['improvement_id']));store.db.commit()
    root=store.output_root/'Improvements'/plan['improvement_id'];root.mkdir(parents=True,exist_ok=True)
    atomic_json(root/'improvement.json',{**plan,'status':status})

class ProductionTelemetry:
    def __init__(self,path):self.path=path;self.samples=[];self.stop=threading.Event()
    def __enter__(self):
        def monitor():
            while not self.stop.is_set():
                try:self.samples.append(gpu_sample())
                except Exception as e:self.samples.append({'monitor_error':str(e)})
                self.stop.wait(2)
        self.thread=threading.Thread(target=monitor,daemon=True);self.thread.start();return self
    def __exit__(self,*args):
        self.stop.set();self.thread.join(timeout=12);atomic_json(self.path,{'samples':self.samples,'peak_gpu_used_mib':max((s.get('used_mib',0) for s in self.samples),default=None)})

def improve(db,store,parent_id,candidates=2,gpu_seconds=900,disk_mb=2048,prepare_only=False):
    with ProcessLock(store.path.with_suffix('.improvement.lock')):
        plan=prepare_improvement(db,store,parent_id,candidates,gpu_seconds,disk_mb)
        parent=db.job(parent_id);asset=json.loads(parent['request_json']);settings=json.loads(parent['settings_json'])
        outputs={s['name']:db.artifact_dict(s['id']) for s in db.stages(parent_id) if s['status']=='Completed'}
        root=store.output_root/'Improvements'/plan['improvement_id'];cfg=plan['config']
        for slot in plan['slots']:
            if not slot['job_id']:
                child_settings={**settings,'seed':slot['seed'],'reuse_files':reuse_for_stage(outputs,plan['target_stage'])}
                if plan['target_stage']=='ReferenceGenerated':
                    child_settings.update(prompt=plan['refinement']['refined_prompt'],style=plan['refinement']['style'],negative=plan['refinement']['negative'],prompt_language='en')
                    child_settings['variant_prompt']='Front-left three-quarter composition, roof and entrance visible.' if slot['slot']=='A' else 'Front-right three-quarter composition, roof and entrance visible.'
                child_asset={**asset,'quality_parent_job':parent_id,'phase31_improvement':plan['improvement_id'],'candidate_slot':slot['slot']}
                # Stable request/seed/slot, never new_version=True: a crash after DB
                # enqueue but before sidecar commit recovers the same job.
                child,_=db.enqueue(child_asset,'phase31_'+slot['slot'],child_settings)
                slot['job_id']=child['id'];save_improvement(store,plan,'Prepared')
        if prepare_only:return {**plan,'status':'Prepared','inference_started':False}
        production_started=False;quality_started=0
        from .quality_config import load_quality
        profile=load_quality(cfg['quality']);expected_prompt='qwen-mesh-evidence-3.2' if profile.get('protocol')=='qwen-evidence-3.2' else PROMPT_VERSION
        baseline_report=json.loads(store.get(plan['baseline_evaluation_id'])['report_json'] or '{}')
        if baseline_report.get('prompt_version')!=expected_prompt:
            # A protocol repair requires new visual evaluations, never new meshes.
            new_baseline=QualityService(db,store).evaluate(parent_id,{'quality':cfg['quality']})
            quality_started+=1
            if new_baseline.get('error'):
                plan['baseline_evaluation_error']=new_baseline['error'];save_improvement(store,plan,'NeedsReview')
                return {**plan,'status':'NeedsReview','error':new_baseline['error'],'inference_started':False,'quality_evaluations_started':quality_started}
            plan.pop('baseline_evaluation_error',None)
            plan.setdefault('evaluation_history',[]).append(plan['baseline_evaluation_id']);plan['baseline_evaluation_id']=new_baseline['evaluation_id']
            save_improvement(store,plan,'Running')
        for slot in plan['slots']:
            job=db.job(slot['job_id']);jobs=[s['job_id'] for s in plan['slots']]
            if job['status']=='Completed':
                stages=[s for s in db.stages(job['id']) if s['status']=='Completed']
                if not {'Scaled','Validated','Packaged'}<={s['name'] for s in stages} or not all(intact(i) for s in stages for i in db.artifact_dict(s['id']).values()):
                    slot['status']='NeedsReview';slot['error']='Completed artifact integrity check failed; no regeneration performed';save_improvement(store,plan,'NeedsReview');continue
            remaining=cfg['gpu_seconds']-used_production_seconds(db,jobs)
            total_size=sum(directory_size(db.job(j)['output_dir']) for j in jobs)
            if job['status']!='Completed':
                if job['status']=='Failed':
                    slot['status']='Failed';save_improvement(store,plan,'NeedsReview');continue
                if remaining<=0 or total_size>=cfg['disk_mb']*1024**2:
                    slot['status']='BudgetStopped';save_improvement(store,plan,'BudgetStopped');break
                print('Improve candidate',slot['slot'],slot['job_id'],flush=True)
                production_started=True
                with ProductionTelemetry(root/('candidate-'+slot['slot']+'-production-gpu.json')):
                    job=BudgetedWorker(db,time.monotonic()+remaining,cfg['disk_mb']*1024**2-total_size,allow_gpu=True).run(slot['job_id'])
                slot['status']=job['status'];save_improvement(store,plan,'Running')
                if job['status']=='NeedsReview':break
            old_evaluation=json.loads(store.get(slot['evaluation_id'])['report_json'] or '{}') if slot['evaluation_id'] else {}
            if job['status']=='Completed' and old_evaluation.get('prompt_version')!=expected_prompt:
                result=QualityService(db,store).evaluate(job['id'],{'quality':cfg['quality']})
                quality_started+=1
                if slot['evaluation_id']:slot.setdefault('evaluation_history',[]).append(slot['evaluation_id'])
                slot['evaluation_id']=result['evaluation_id'];slot['visual_decision']=result['decision'];save_improvement(store,plan,'Running')
                slot['evaluation_error']=result.get('error')
        status='Completed' if all(s.get('evaluation_id') and not s.get('evaluation_error') and not s.get('error') and db.job(s['job_id'])['status']=='Completed' for s in plan['slots']) else 'NeedsReview'
        plan['visual_acceptance']='NotApproved';plan['quality_improvement_human_verified']=False
        save_improvement(store,plan,status)
        return {**plan,'status':status,'inference_started':production_started,'quality_evaluations_started':quality_started,'manual_approval_granted':False}
