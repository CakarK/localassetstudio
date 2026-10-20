"""Three isolated reference-only jobs, existing gates, immutable evidence selection."""
import json
import uuid
from pathlib import Path
from .common import AI,ProcessLock,ProductionError,atomic_json,canonical,digest,intact,utcnow
from .worker import Worker,settings_for
from .quality_gates import QualityGateService,current_gate,outputs_for
from .reference_prompts import compile_prompt

SELECTION_VERSION='reference-evidence-selection-3.3.0'

def sidecar(store):return {'db':str(store.path),'root':str(store.output_root)}
def save_set(store,plan,status):
    store.db.execute('UPDATE ReferenceSets SET status=?,plan_json=? WHERE id=?',(status,canonical(plan),plan['set_id']));store.db.commit()
    atomic_json(store.output_root/'References'/plan['set_id']/'reference-set.json',{**plan,'status':status})

def get_set(store,asset_id):
    row=store.db.execute('SELECT * FROM ReferenceSets WHERE asset_id=?',(asset_id,)).fetchone()
    if not row:raise ProductionError('ReferenceSetMissing','Generate reference candidates first')
    return json.loads(row['plan_json'])

def candidates(store,set_id):return [dict(r) for r in store.db.execute('SELECT * FROM ReferenceCandidates WHERE set_id=? ORDER BY slot',(set_id,))]

def verify_candidate(db,store,row):
    outputs=outputs_for(db,row['job_id']);meta=json.loads(row['metadata_json']);expected=meta.get('artifacts',{})
    if not expected:raise ProductionError('ReferenceIncomplete','Candidate has no completed reference checkpoint')
    for stage,key in [('ReferenceGenerated','reference'),('ReferenceProcessed','preprocessed')]:
        info=outputs.get(stage,{}).get(key)
        if not info or info!=expected.get(key) or not intact(info):raise ProductionError('ReferenceIntegrity','Reference or processed mask changed; no regeneration permitted')
    gate=current_gate(db,store,row['job_id'],'reference')
    return outputs,gate

def comparison(db,store,asset_id):
    plan=get_set(store,asset_id);items=[]
    for row in candidates(store,plan['set_id']):
        item={'candidate_id':row['id'],'job_id':row['job_id'],'slot':row['slot'],'seed':row['seed'],'gate_status':'NeedsReview','eligible':False}
        try:
            outputs,gate=verify_candidate(db,store,row)
            item['artifacts']={k:outputs[s][k] for s,k in [('ReferenceGenerated','reference'),('ReferenceProcessed','preprocessed')]}
            if gate:
                item.update(gate_status=gate['status'],gate_report_id=gate['report_id'],eligible=gate['status']=='PASS')
                # No alpha-max ranking: whole-object evidence first; confidence is
                # only an uncalibrated support level, never a probability.
                criteria=gate.get('vlm',{}).get('criteria',{});priority=['whole_object','expected_identity','framing','mask_preserves_object','shape_suitability','category_match']
                item['evidence_support']=[criteria.get(k,{}).get('confidence',0) if criteria.get(k,{}).get('result')=='pass' else 0 for k in priority]
                item['evidence']={k:criteria.get(k) for k in priority};metrics=gate['deterministic']['metrics']
                center=metrics.get('centroid_normalized',[0,0]);item['composition_support']=[metrics.get('largest_component_fraction',0),-max(abs(center[0]-.5),abs(center[1]-.5)),-metrics.get('edge_contact_fraction',1)]
                item['foreground_fraction']=metrics['foreground_fraction']
        except ProductionError as exc:item['error']={'category':exc.category,'message':str(exc)}
        items.append(item)
    passing=[i for i in items if i['eligible']]
    ranked=sorted(passing,key=lambda i:(i['evidence_support'],i['composition_support'],-i['slot']),reverse=True)
    conflicting=any(i.get('evidence') and any(v and v['result']!='pass' for v in i['evidence'].values()) for i in passing)
    return {'set_id':plan['set_id'],'asset_id':asset_id,'algorithm_version':SELECTION_VERSION,'candidates':items,
            'pass_count':len(passing),'recommended_reference_id':ranked[0]['candidate_id'] if ranked and not conflicting else None,
            'status':'PASS' if ranked and not conflicting else 'NeedsReview','confidence_is_probability':False,
            'reason':'PASS-only lexicographic whole-object/identity/framing/mask evidence, then connected foreground/centering/edge clearance; stable slot breaks exact ties.' if ranked and not conflicting else 'No current unambiguous PASS reference; no Shape authorized.'}

def select_reference(db,store,asset_id):
    report=comparison(db,store,asset_id);key=digest(report)
    prior=store.db.execute('SELECT report_json FROM ReferenceSelections WHERE selection_key=?',(key,)).fetchone()
    if prior:return json.loads(prior[0])
    report.update(selection_id=uuid.uuid4().hex,selection_key=key,selected_reference_id=report['recommended_reference_id'],created_at=utcnow(),
                  rejected_candidates=[{'candidate_id':c['candidate_id'],'gate_status':c['gate_status'],'reason':c.get('error') or ('Gate did not PASS' if not c['eligible'] else 'Lower or tied evidence ranking')} for c in report['candidates'] if c['candidate_id']!=report['recommended_reference_id']])
    store.db.execute('INSERT INTO ReferenceSelections VALUES (?,?,?,?,?)',(report['selection_id'],report['set_id'],key,report['created_at'],canonical(report)));store.db.commit()
    plan=get_set(store,asset_id);plan['selection_id']=report['selection_id'];save_set(store,plan,'Selected' if report['selected_reference_id'] else 'NeedsReview')
    atomic_json(store.output_root/'References'/plan['set_id']/('selection-'+report['selection_id']+'.json'),report);return report

class ReferenceService:
    def __init__(self,db,store,worker_factory=Worker,gate_factory=QualityGateService,model_check=None,monitor_gpu=True):
        self.db=db;self.store=store;self.worker_factory=worker_factory;self.gate_factory=gate_factory;self.model_check=model_check;self.monitor_gpu=monitor_gpu
    def model_preflight(self):
        if self.model_check:return self.model_check()
        from .quality_vlm import OllamaVLM
        return OllamaVLM().metadata()
    def prepare(self,asset_id,count=3):
        if type(count) is not int or not 1<=count<=3:raise ValueError('At most three reference candidates are permitted')
        asset={k:v for k,v in self.db.asset(asset_id).items() if k not in ('jobs','status')};prompt=compile_prompt(asset)
        from .quality_config import load_quality
        vlm_profile=load_quality();request_key=digest({'asset':asset,'count':count,'prompt':prompt,'vlm_profile':vlm_profile})
        prior=self.store.db.execute('SELECT plan_json FROM ReferenceSets WHERE asset_id=?',(asset_id,)).fetchone()
        if prior:
            plan=json.loads(prior[0])
            if plan['request_key']!=request_key:raise ProductionError('ReferenceBudgetFixed','Existing campaign/source/count already owns this asset budget; no extra references created')
            return plan
        ident=uuid.uuid4().hex;settings=settings_for(asset,{'max_transient_retries':0,'allow_quality_reduction':False})
        plan={'set_id':ident,'asset_id':asset_id,'request_key':request_key,'asset':asset,'prompt':prompt,'settings':settings,'vlm_profile':vlm_profile,
              'budget':{'references':count,'shape':1,'pbr':1,'final_evaluations':1},
              'slots':[{'slot':n+1,'candidate_id':uuid.uuid4().hex,'seed':settings['seed']+(n+1)*1009} for n in range(count)],'created_at':utcnow()}
        self.store.db.execute('INSERT INTO ReferenceSets VALUES (?,?,?,?,?,?)',(ident,asset_id,request_key,'Prepared',plan['created_at'],canonical(plan)));self.store.db.commit();save_set(self.store,plan,'Prepared');return plan
    def generate(self,asset_id,count=3):
        with ProcessLock(self.store.path.with_suffix('.references.lock')):
            plan=self.prepare(asset_id,count);self.model_preflight()
            for slot in plan['slots']:
                row=self.store.db.execute('SELECT * FROM ReferenceCandidates WHERE set_id=? AND slot=?',(plan['set_id'],slot['slot'])).fetchone()
                if row:
                    row=dict(row)
                    if json.loads(row['metadata_json']).get('artifacts'):
                        verify_candidate(self.db,self.store,row);continue
                request={**plan['asset'],'phase33_set_id':plan['set_id'],'reference_candidate_id':slot['candidate_id'],'reference_slot':slot['slot'],
                         'production_role':'reference_candidate','phase33_sidecar':sidecar(self.store),'phase33_budget':plan['budget']}
                settings={**plan['settings'],'seed':slot['seed'],'prompt':plan['prompt']['generation_prompt'],'negative':plan['prompt']['negative'],
                          'style':plan['asset']['shared_style'],'variant_prompt':['Front-left elevated three-quarter camera, whole building.','Front-right elevated three-quarter camera, whole building.','Elevated three-quarter camera, complete body and entrance visible.'][slot['slot']-1]}
                job,_=self.db.enqueue(request,'phase33_reference',settings)
                if not row:
                    meta={'generation_prompt_version':plan['prompt']['version'],'source_prompt_sha256':plan['prompt']['source_sha256'],'budget':plan['budget']}
                    self.store.db.execute('INSERT INTO ReferenceCandidates VALUES (?,?,?,?,?,?,?)',(slot['candidate_id'],plan['set_id'],slot['slot'],slot['seed'],job['id'],None,canonical(meta)));self.store.db.commit()
                else:meta=json.loads(row['metadata_json'])
                if self.db.job(job['id'])['status']=='Failed':continue
                from contextlib import nullcontext
                from .quality_improve import ProductionTelemetry
                telemetry=self.store.output_root/'References'/plan['set_id']/('candidate-'+str(slot['slot'])+'-production-gpu.json')
                with ProductionTelemetry(telemetry) if self.monitor_gpu else nullcontext():
                    result=self.worker_factory(self.db,allow_gpu=True).run(job['id'])
                outputs=outputs_for(self.db,job['id']);meta['job_status']=result['status']
                if all(s in outputs for s in ('ReferenceGenerated','ReferenceProcessed')):
                    meta['artifacts']={'reference':outputs['ReferenceGenerated']['reference'],'preprocessed':outputs['ReferenceProcessed']['preprocessed']}
                self.store.db.execute('UPDATE ReferenceCandidates SET metadata_json=? WHERE id=?',(canonical(meta),slot['candidate_id']));self.store.db.commit()
                atomic_json(self.store.output_root/'References'/plan['set_id']/('candidate-'+str(slot['slot'])+'.json'),{**slot,'job_id':job['id'],**meta})
            # A cached generate must not regress a selected campaign's state.
            if not plan.get('selection_id'):save_set(self.store,plan,'ReferencesGenerated')
            return {'set_id':plan['set_id'],'asset_id':asset_id,'candidates':candidates(self.store,plan['set_id']),'shape_started':False}
    def evaluate(self,asset_id):
        with ProcessLock(self.store.path.with_suffix('.references.lock')):
            plan=get_set(self.store,asset_id);self.model_preflight();reports=[]
            from .quality_config import load_quality
            if load_quality()!=plan['vlm_profile']:raise ProductionError('VLMConfigurationMismatch','Reference campaign profile changed; no inference submitted')
            for row in candidates(self.store,plan['set_id']):
                try:verify_candidate(self.db,self.store,row)
                except ProductionError as exc:
                    reports.append({'candidate_id':row['id'],'status':'NeedsReview','error':{'category':exc.category,'message':str(exc)}});continue
                report=self.gate_factory(self.db,self.store).run('reference',row['job_id'])
                self.store.db.execute('UPDATE ReferenceCandidates SET gate_report_id=? WHERE id=?',(report['report_id'],row['id']));self.store.db.commit();reports.append(report)
            return {'set_id':plan['set_id'],'reports':reports,'comparison':comparison(self.db,self.store,asset_id),'shape_started':False}
