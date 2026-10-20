"""One selected reference -> one Shape -> gated PBR, with durable budget claims."""
import json
import time
import uuid
from pathlib import Path
import requests
from .common import AI,ProcessLock,ProductionError,atomic_json,canonical,digest,intact,utcnow
from .quality_store import QualityStore
from .quality_gates import current_gate,outputs_for,gate_summary
from .quality_config import load_quality
from .reference_candidates import get_set,candidates,verify_candidate,sidecar
from .quality_vlm import OllamaVLM,gpu_sample,check_vlm_reservation
from .worker import Worker

def claim_budget(store,scope,operation,slot,owner):
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row=store.db.execute('SELECT owner_attempt FROM ProductionBudgetClaims WHERE scope_id=? AND operation=? AND slot=?',(scope,operation,slot)).fetchone()
        if row and row[0]!=owner:raise ProductionError('ProductionBudgetExhausted','One '+operation+' submission already belongs to another attempt; no second inference')
        if not row:store.db.execute('INSERT INTO ProductionBudgetClaims VALUES (?,?,?,?,?)',(scope,operation,slot,owner,utcnow()))
        store.db.commit()
    except BaseException:store.db.rollback();raise

def guard_gpu_submission(db,asset,stage,attempt,settings):
    store=QualityStore(asset['phase33_sidecar']['db'],asset['phase33_sidecar']['root'])
    try:
        plan=get_set(store,asset['asset_id'])
        if plan['set_id']!=asset['phase33_set_id']:raise ProductionError('ProductionBudgetMismatch','Wrong campaign')
        if asset['production_role']=='reference_candidate':
            slot=asset['reference_slot']
            if stage!='ReferenceGenerated' or not any(s['slot']==slot and s['candidate_id']==asset['reference_candidate_id'] for s in plan['slots']):
                raise ProductionError('ProductionBudgetExhausted','Reference-only job cannot run Shape/PBR')
            operation='reference'
        else:
            if stage not in ('GeometryGenerated','PBRGenerated'):raise ProductionError('ProductionBudgetExhausted','Controlled asset cannot generate extra reference')
            operation='shape' if stage=='GeometryGenerated' else 'pbr';slot=0
            controlled=store.db.execute('SELECT job_id FROM ControlledGenerations WHERE set_id=?',(plan['set_id'],)).fetchone()
            if not controlled or controlled[0]!=attempt['job_id']:raise ProductionError('ProductionBudgetMismatch','Attempt not owned by this controlled job')
            required='reference' if operation=='shape' else 'geometry';gate=current_gate(db,store,attempt['job_id'],required)
            if not gate or gate['status']!='PASS':raise ProductionError('ProductionGateBlocked','Current '+required+' PASS required before GPU submission')
        # Recovery of a known remote attempt is not a new submission. Existing
        # ComfyClient resolves its client token/history without another POST.
        if attempt['state'] not in ('Submitting','Submitted','SubmissionUnknown','NeedsReview'):
            check_vlm_reservation();profile=load_quality()
            ps=requests.get(profile['url']+'/api/ps',timeout=10);ps.raise_for_status()
            if ps.json().get('models'):raise ProductionError('GPUBusy','Ollama is resident; nothing submitted or unloaded')
            q=requests.get(settings['comfy_url']+'/queue',timeout=10);q.raise_for_status();queue=q.json()
            if queue.get('queue_running') or queue.get('queue_pending'):raise ProductionError('GPUBusy','Foreign Comfy work preserved; no submission')
            response=requests.post(settings['comfy_url']+'/free',json={'unload_models':True,'free_memory':True},timeout=20);response.raise_for_status()
            sample=gpu_sample()
            for _ in range(5):
                if sample['free_mib']>=12000:break
                time.sleep(.5);sample=gpu_sample()
            if sample['free_mib']<12000:raise ProductionError('InsufficientVRAM','Controlled GPU operation requires 12000 MiB free; no silent reduction')
            atomic_json(Path(attempt['work_dir'])/'production-gpu-preflight.json',{'queue_empty':True,'ollama_resident':False,'gpu':sample,'minimum_free_mib':12000})
        claim_budget(store,plan['set_id'],operation,slot,attempt['id'])
    finally:store.close()

def save_control(store,row,status):
    row['status']=status
    store.db.execute('UPDATE ControlledGenerations SET job_id=?,evaluation_id=?,status=?,metadata_json=? WHERE id=?',
        (row.get('job_id'),row.get('evaluation_id'),status,canonical(row),row['controlled_id']));store.db.commit()
    atomic_json(store.output_root/'Controlled'/row['controlled_id']/'controlled-generation.json',row)

def baseline_for(db,store,asset_id):
    roots=[j for j in db.jobs() if j['asset_id']==asset_id and j['status']=='Completed' and not json.loads(j['request_json']).get('quality_parent_job') and not json.loads(j['request_json']).get('production_role')]
    for job in roots:
        full=store.gate_report(job['id'],'full')
        if full and full.get('visual_evaluation_id'):return {'job_id':job['id'],'evaluation_id':full['visual_evaluation_id']}
    return None

def traceability(db,store,row):
    plan=get_set(store,row['asset_id']);candidate=next(c for c in candidates(store,plan['set_id']) if c['id']==row['reference_candidate_id'])
    selection=json.loads(store.db.execute('SELECT report_json FROM ReferenceSelections WHERE id=?',(row['selection_id'],)).fetchone()[0])
    reference_stages=[{'stage':s['name'],'stage_id':s['id'],'parameters':json.loads(s['parameters_json']),'inputs':json.loads(s['inputs_json']),'outputs':db.artifact_dict(s['id'])} for s in db.stages(candidate['job_id']) if s['status']=='Completed']
    evaluation=json.loads(store.get(row['evaluation_id'])['report_json'] or '{}') if row.get('evaluation_id') else {}
    checkpoints=[]
    for s in db.stages(row['job_id']):
        if s['status']=='Completed':checkpoints.append({'stage':s['name'],'stage_id':s['id'],'fingerprint':s['fingerprint'],'inputs':json.loads(s['inputs_json']),'outputs':db.artifact_dict(s['id'])})
    report={'version':'reference-to-mesh-3.3.0','set_id':plan['set_id'],'controlled_id':row['controlled_id'],'job_id':row['job_id'],'asset_id':row['asset_id'],
        'source_prompt_sha256':plan['prompt']['source_sha256'],'generation_prompt_version':plan['prompt']['version'],'generation_prompt_sha256':digest(plan['prompt']),
        'reference_candidate_id':candidate['id'],'reference_job_id':candidate['job_id'],'reference_artifacts':json.loads(candidate['metadata_json']).get('artifacts'),
        'reference_gate_report_id':candidate['gate_report_id'],'selection':selection,'budget':plan['budget'],'checkpoints':checkpoints,
        'reference_stages':reference_stages,'source_reference_gate':current_gate(db,store,candidate['job_id'],'reference'),
        'geometry_gate':current_gate(db,store,row['job_id'],'geometry'),'promoted_reference_gate':current_gate(db,store,row['job_id'],'reference'),
        'visual_evaluation_id':row.get('evaluation_id'),'status':row['status'],'no_human_approval_inferred':True}
    report['visual_evidence']={k:evaluation.get(k) for k in ('model','model_version','protocol_sha256','source_model','render_artifacts')}
    atomic_json(store.output_root/'Controlled'/row['controlled_id']/'traceability.json',report);return report

class ControlledService:
    def __init__(self,db,store,worker_factory=Worker,model_check=None):self.db=db;self.store=store;self.worker_factory=worker_factory;self.model_check=model_check
    def run(self,asset_id):
        with ProcessLock(self.store.path.with_suffix('.references.lock')):
            plan=get_set(self.store,asset_id)
            if load_quality()!=plan['vlm_profile']:raise ProductionError('VLMConfigurationMismatch','Controlled campaign VLM profile changed')
            if self.model_check:self.model_check()
            else:OllamaVLM().metadata()
            if not plan.get('selection_id'):raise ProductionError('ReferenceSelectionMissing','Select a current PASS reference first')
            selection=json.loads(self.store.db.execute('SELECT report_json FROM ReferenceSelections WHERE id=?',(plan['selection_id'],)).fetchone()[0]);chosen=selection.get('selected_reference_id')
            if not chosen:raise ProductionError('ReferenceGateBlocked','No PASS selection; zero Shape submissions')
            candidate=next((c for c in candidates(self.store,plan['set_id']) if c['id']==chosen),None)
            if not candidate:raise ProductionError('ReferenceSelectionMissing','Selected candidate missing')
            reference_outputs,gate=verify_candidate(self.db,self.store,candidate)
            if not gate or gate['status']!='PASS' or gate['report_id']!=next(c['gate_report_id'] for c in selection['candidates'] if c['candidate_id']==chosen):
                raise ProductionError('ReferenceGateBlocked','Selected reference gate is missing, changed or not PASS')
            previous=self.store.db.execute('SELECT metadata_json FROM ControlledGenerations WHERE set_id=?',(plan['set_id'],)).fetchone()
            if previous:
                row=json.loads(previous[0])
                if row['reference_candidate_id']!=chosen:raise ProductionError('ProductionBudgetFixed','This campaign already owns one selected Shape; cannot switch candidates')
            else:
                row={'controlled_id':uuid.uuid4().hex,'set_id':plan['set_id'],'asset_id':asset_id,'reference_candidate_id':chosen,'selection_id':selection['selection_id'],
                     'job_id':None,'evaluation_id':None,'budget':plan['budget'],'baseline':baseline_for(self.db,self.store,asset_id),'created_at':utcnow()}
                self.store.db.execute('INSERT INTO ControlledGenerations VALUES (?,?,?,?,?,?,?)',(row['controlled_id'],plan['set_id'],None,None,'Prepared',canonical(row),row['created_at']));self.store.db.commit()
            if not row['job_id']:
                request={**plan['asset'],'production_role':'controlled_asset','phase33_set_id':plan['set_id'],'phase33_sidecar':sidecar(self.store),
                    'selected_reference_id':chosen,'selected_reference_gate_id':gate['report_id'],'phase33_budget':plan['budget']}
                settings={**plan['settings'],'seed':plan['settings']['seed']+9001,'shape_resolution':384,'shape_steps':40,'polygon_target':plan['settings']['polygon_target'],'texture_size':plan['settings']['texture_size'],
                    'reuse_files':{s:{k:v for k,v in reference_outputs[s].items() if k!='origin'} for s in ('ReferenceGenerated','ReferenceProcessed')}}
                job,_=self.db.enqueue(request,'phase33_controlled',settings);row['job_id']=job['id'];save_control(self.store,row,'Prepared')
            job=self.db.job(row['job_id']);production_started=False
            if job['status']=='Completed':
                if not all(intact(i) for s in self.db.stages(job['id']) if s['status']=='Completed' for i in self.db.artifact_dict(s['id']).values()):
                    raise ProductionError('ArtifactIntegrity','Completed artifact changed; no new Shape allowed')
            elif job['status'] not in ('Failed','NeedsReview','Cancelled'):
                from .quality_improve import ProductionTelemetry
                production_started=True
                with ProductionTelemetry(self.store.output_root/'Controlled'/row['controlled_id']/'production-gpu.json'):
                    job=self.worker_factory(self.db,allow_gpu=True).run(job['id'])
                save_control(self.store,row,job['status'])
            if job['status']=='Completed' and not row.get('evaluation_id'):
                # Persist reservation and evaluation identity before inference. A
                # crash in the handoff is NeedsReview, never a second evaluation.
                claim_budget(self.store,plan['set_id'],'final_evaluation',0,row['controlled_id'])
                reserved=digest({'controlled':row['controlled_id'],'operation':'final-evaluation'})[:32]
                existing=self.store.db.execute('SELECT id FROM QualityEvaluations WHERE id=?',(reserved,)).fetchone()
                evaluation=self.store.begin(job,{},ident=reserved)
                row['evaluation_id']=evaluation['id'];save_control(self.store,row,'Evaluating')
                if existing:save_control(self.store,row,'NeedsReview')
                else:
                    from .quality_service import QualityService
                    result=QualityService(self.db,self.store).evaluate(job['id'],prepared_evaluation=evaluation)
                    save_control(self.store,row,'Completed' if result.get('scores') else 'NeedsReview')
            elif row.get('evaluation_id'):
                evaluation=self.store.get(row['evaluation_id'])
                if not evaluation['report_json']:save_control(self.store,row,'NeedsReview')
                else:save_control(self.store,row,'Completed' if json.loads(evaluation['report_json']).get('scores') else 'NeedsReview')
            trace=traceability(self.db,self.store,row)
            return {**row,'production_status':job['status'],'production_started':production_started,'manual_approval':False,'traceability':str(self.store.output_root/'Controlled'/row['controlled_id']/'traceability.json')}
