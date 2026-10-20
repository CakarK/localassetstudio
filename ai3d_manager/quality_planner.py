"""Evidence-bound plans; no destructive mesh editing and no implicit inference."""
import json
import math
import shutil
import time
import uuid
from pathlib import Path
from .common import ProductionError, atomic_json, canonical, utcnow
from .worker import Worker, GPU_STAGES

RETRY_DEFAULTS={'enabled':False,'max_candidates':3,'max_gpu_seconds':600,'max_disk_mb':2048}

def plan_corrections(report):
    actions=[]
    for finding in report.get('findings',[]):
        text=finding['problem'].lower();category=finding['category'];target='ReferenceGenerated';kind='ReferenceCorrection';automatic=True
        if category=='texture':target='PBRGenerated';kind='TextureCorrection'
        elif category=='geometry':
            if any(word in text for word in ('platform','plinth','ground','base')):
                kind='BlenderPostprocessCorrection';target='BlenderExported';automatic=False
            else:kind='GeometryCorrection';target='GeometryGenerated'
        elif category=='art_style' or category=='top_down_readability':kind='ReferenceCorrection'
        actions.append({'id':'FIX'+str(len(actions)+1).zfill(2),'category':kind,'target_stage':target,'criterion_id':finding['criterion_id'],
            'observation':finding['problem'],'evidence_images':finding['evidence_images'],'confidence':finding['confidence'],'severity':finding.get('severity','unknown'),
            'suggestion':finding['suggested_fix'],'automatic_execution_supported':automatic,
            'safety_note':'Do not delete an integrated platform or repair thin geometry without a reviewed mesh separation plan; preserve the original.' if not automatic else 'Fresh candidate version only; original artifacts are reused read-only where applicable.'})
    for requirement in report.get('requirements',[]):
        if requirement['type']=='technical' and requirement.get('result')=='FAIL':
            actions.append({'id':'FIX'+str(len(actions)+1).zfill(2),'category':'BlenderPostprocessCorrection','target_stage':'Scaled',
                'criterion_id':requirement['id'],'observation':'Technical validator failed: '+requirement['description'],
                'evidence_images':[],'technical_evidence':requirement.get('technical_evidence'),'confidence':None,
                'suggestion':'Review authoritative grid/height/pivot settings, then re-export a new version; do not infer dimensions from images.',
                'automatic_execution_supported':False,'safety_note':'Requires reviewed numeric settings, no automatic mesh cutting or guessed scale.'})
    return {'version':'correction-plan-1.0','job_id':report['job_id'],'evaluation_id':report['evaluation_id'],
            'automatic_retry':dict(RETRY_DEFAULTS),'actions':actions,'state':'SuggestionsOnly','created_at':utcnow()}

def save_plan(store,report):
    plan=plan_corrections(report);ident=uuid.uuid4().hex;plan['plan_id']=ident
    store.db.execute('INSERT INTO QualityPlans VALUES (?,?,?,?)',(ident,report['evaluation_id'],utcnow(),canonical(plan)));store.db.commit()
    atomic_json(Path(store.get(report['evaluation_id'])['work_dir'])/('correction-plan-'+ident+'.json'),plan)
    return plan

def retry_policy(overrides=None):
    p={**RETRY_DEFAULTS,**(overrides or {})}
    if type(p['enabled']) is not bool:raise ValueError('enabled must be a boolean')
    if type(p['max_candidates']) is not int or not 1<=p['max_candidates']<=3:raise ValueError('At most three candidates are permitted')
    if any(not isinstance(p[k],(int,float)) or not math.isfinite(p[k]) or p[k]<=0 for k in ('max_gpu_seconds','max_disk_mb')):raise ValueError('Finite positive GPU/disk budgets required')
    return p

def directory_size(path):return sum(p.stat().st_size for p in Path(path).rglob('*') if p.is_file())

def enqueue_candidates(db,store,plan,options=None,action_id=None):
    p=retry_policy(options)
    if not p['enabled']:raise ProductionError('RetryDisabled','Automatic inference retry is disabled; explicit --enable-inference is required even to prepare candidate jobs.')
    actions=[a for a in plan['actions'] if a['automatic_execution_supported'] and (not action_id or a['id']==action_id)]
    if not actions:raise ValueError('No safely supported correction action selected; manual mesh edits are not automatic')
    # Fidelity/reference changes take precedence over texture-only guesses.
    selected=min(actions,key=lambda a:['ReferenceGenerated','GeometryGenerated','PBRGenerated'].index(a['target_stage']))
    job=db.job(plan['job_id']);asset=json.loads(job['request_json']);settings=json.loads(job['settings_json'])
    outputs={s['name']:db.artifact_dict(s['id']) for s in db.stages(job['id']) if s['status']=='Completed'}
    keep={'ReferenceGenerated':[], 'GeometryGenerated':['ReferenceGenerated','ReferenceProcessed'],
          'PBRGenerated':['ReferenceGenerated','ReferenceProcessed','GeometryGenerated','GeometryOptimized']}[selected['target_stage']]
    reusable={stage:{k:v for k,v in outputs[stage].items() if k!='origin'} for stage in keep}
    created=[]
    for number in range(1,p['max_candidates']+1):
        prior=store.db.execute('SELECT * FROM QualityCandidates WHERE plan_id=? AND candidate_number=?',(plan['plan_id'],number)).fetchone()
        if prior:created.append(dict(prior));continue
        if shutil.disk_usage(db.output_root.parent if db.output_root.parent.exists() else db.path.parent).free<p['max_disk_mb']*1024**2:
            raise ProductionError('DiskBudget','Insufficient disk space for configured candidate budget')
        candidate_settings={**settings,'seed':settings['seed']+number,'reuse_files':reusable,
            'comfy_timeout':min(settings['comfy_timeout'],p['max_gpu_seconds'])}
        if selected['category']=='ReferenceCorrection':candidate_settings['variant_prompt']='Prioritize source requirement: '+selected['suggestion']
        candidate_asset={**asset,'quality_parent_job':job['id'],'quality_plan':plan['plan_id'],'quality_budget':p,
                         'quality_action':selected,'quality_candidate_number':number}
        variant=job['variant'][:55]+'_quality_'+str(number)
        child,_=db.enqueue(candidate_asset,variant,candidate_settings,new_version=True)
        ident=uuid.uuid4().hex
        store.db.execute('INSERT INTO QualityCandidates VALUES (?,?,?,?,?,?,?,?)',(ident,job['id'],plan['plan_id'],number,child['id'],'Enqueued',canonical(candidate_asset),utcnow()));store.db.commit()
        created.append(dict(store.db.execute('SELECT * FROM QualityCandidates WHERE id=?',(ident,)).fetchone()))
    return {'policy':p,'selected_action':selected,'candidates':created,'inference_started':False}

class BudgetedWorker(Worker):
    def __init__(self,db,deadline,disk_limit,**kwargs):super().__init__(db,**kwargs);self.deadline=deadline;self.disk_limit=disk_limit
    def boundary(self,ident):
        super().boundary(ident)
        if time.monotonic()>self.deadline:raise ProductionError('GPUTimeBudget','Candidate time budget reached; no new stage submitted')
        if directory_size(self.db.job(ident)['output_dir'])>self.disk_limit:raise ProductionError('DiskBudget','Candidate disk budget reached; artifacts retained, no new stage submitted')
    def execute_stage(self,stage,attempt,asset,settings):
        if stage in GPU_STAGES and stage not in settings.get('reuse_files',{}):
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise ProductionError('GPUTimeBudget','No inference budget remains')
            settings={**settings,'comfy_timeout':min(settings['comfy_timeout'],remaining)}
        return super().execute_stage(stage,attempt,asset,settings)

def run_candidates(db,store,prepared):
    p=prepared['policy'];deadline=time.monotonic()+p['max_gpu_seconds'];results=[]
    # Soft stage-boundary budgets: never interrupt a foreign job or blindly retry a
    # timed-out remote inference. One in-flight stage may outlast its reservation.
    for candidate in prepared['candidates']:
        if time.monotonic()>=deadline:break
        total=sum(directory_size(db.job(c['job_id'])['output_dir']) for c in prepared['candidates'])
        if total>=p['max_disk_mb']*1024**2:break
        w=BudgetedWorker(db,deadline,p['max_disk_mb']*1024**2-total,allow_gpu=True)
        result=w.run(candidate['job_id']);results.append(result)
        store.db.execute('UPDATE QualityCandidates SET status=? WHERE id=?',(result['status'],candidate['id']));store.db.commit()
        if result['status'] in ('NeedsReview','Failed'):break
    return results

def compare_candidates(db,store,asset_id):
    latest={}
    for evaluation in store.evaluations(asset_id):latest[evaluation['job_id']]=evaluation
    # A phase 3.1 experiment pins the exact benchmark protocol for its baseline.
    # Do not compare it with the later, different split-batch benchmark trial.
    experiment=None
    for row in store.db.execute('SELECT plan_json FROM QualityImprovements ORDER BY created_at DESC'):
        plan=json.loads(row[0])
        if plan['asset_id']==asset_id and any(s.get('evaluation_id') for s in plan['slots']):experiment=plan;break
    if experiment:
        latest={experiment['parent_job_id']:store.get(experiment['baseline_evaluation_id'])}
        for slot in experiment['slots']:
            if slot.get('evaluation_id'):latest[slot['job_id']]=store.get(slot['evaluation_id'])
    # Phase 3.2 full checks pin fresh, same-protocol Qwen evaluations, independently
    # of historical Phase 3.1 Gemma experiment pointers.
    for job in db.jobs():
        if job['asset_id']!=asset_id:continue
        full=store.gate_report(job['id'],'full')
        if full and full.get('visual_evaluation_id'):latest[job['id']]=store.get(full['visual_evaluation_id'])
    controlled=store.db.execute('SELECT metadata_json FROM ControlledGenerations WHERE job_id IS NOT NULL ORDER BY created_at DESC').fetchall()
    for row in controlled:
        run=json.loads(row[0])
        if run['asset_id']==asset_id and run.get('baseline'):
            latest={run['baseline']['job_id']:store.get(run['baseline']['evaluation_id'])}
            latest[run['job_id']]=store.get(run['evaluation_id']) if run.get('evaluation_id') else {'id':None,'job_id':run['job_id'],'report_json':canonical({'decision':'NeedsReview','scores':None,'overall_score':None,'generation_blocked_status':run.get('status')})}
            break
    candidates=[]
    for job_id,e in latest.items():
        report=json.loads(e['report_json'] or '{}');job=db.job(job_id);settings=json.loads(job['settings_json'])
        manual=store.actions(job_id);last=next((a for a in reversed(manual) if a['evaluation_id']==e['id']),None)
        from .quality_gates import gate_summary
        gates=gate_summary(db,store,job_id)
        eligible=gates['status']=='PASS' and report.get('technical_status')=='TechnicalPassed' and report.get('decision')=='VisuallyApprovedCandidate' and not report.get('critical_findings')
        if last and last['action']=='Rejected':eligible=False
        candidates.append({'job_id':job_id,'evaluation_id':e['id'],'decision':report.get('decision','NeedsReview'),
            'technical_status':report.get('technical_status'),'score':report.get('overall_score'),'scores':report.get('scores'),
            'critical_findings':len(report.get('critical_findings',[])),'generation_seconds':sum(
              ( __import__('datetime').datetime.fromisoformat(s['ended_at'])-__import__('datetime').datetime.fromisoformat(s['started_at'])).total_seconds()
              for s in db.stages(job_id) if s['status']=='Completed' and s['ended_at']),
            'polygon_target':settings.get('polygon_target'),'texture_size':settings.get('texture_size'),
            'model_triangles':report.get('render_metadata',{}).get('triangles'),'manual_review':last,'eligible_for_recommendation':eligible,
            'quality_gates':gates,
            'vlm_seconds':report.get('performance',{}).get('vlm',{}).get('elapsed_seconds'),
            'evaluation_seconds':report.get('performance',{}).get('total_seconds'),
            'comparison_signature':(report.get('evaluation_version'),canonical(report.get('policy')),report.get('model_version'),report.get('interpretation_sha256'),report.get('protocol_sha256'))})
    signatures={c['comparison_signature'] for c in candidates}
    comparable=len(signatures)<=1
    eligible=[c for c in candidates if c['eligible_for_recommendation']] if comparable else []
    # Prompt fidelity and overhead readability precede score when differences matter.
    best=max(eligible,key=lambda c:(c['score'],c['scores']['prompt_fidelity'],c['scores']['top_down_readability'])) if eligible else None
    review_candidates=[c for c in candidates if c['technical_status']=='TechnicalPassed' and c['score'] is not None and c['quality_gates']['geometry']['status']=='PASS' and c['quality_gates']['reference']['status']!='FAIL'] if comparable else []
    review_best=max(review_candidates,key=lambda c:c['score']) if review_candidates else None
    return {'asset_id':asset_id,'candidates':candidates,'same_evaluation_protocol':comparable,
            'recommended_job_id':best['job_id'] if best else None,'best_review_candidate_job_id':review_best['job_id'] if review_best else None,'recommendation_is_final_approval':False,
            'note':'Manual review required; critical defects, technical failures and differing evaluation protocols are not auto-selected.'}
