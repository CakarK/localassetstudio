"""Immutable quality sidecars. Existing production stages are never rewritten."""
import json
import shutil
import time
import uuid
from pathlib import Path
from PIL import Image
from .common import AI,ProductionError,atomic_json,canonical,digest,file_info,intact,utcnow
from .gate_analysis import profile_for,reference_analysis,geometry_analysis,decision
from .quality_schema import obj,enum,parse_response
from .quality_vlm import OllamaVLM,GPUQualityLease
from .processes import run_process

VERSION='identity-gates-3.2.0'
VIEWS=['front','back','left','right','isometric_45','top']
RULES={'reference':['expected_identity','whole_object','framing','mask_preserves_object','category_match','shape_suitability'],
       'geometry':['expected_identity','whole_object','structural_components','no_major_fragmentation']}

def gate_schema(kind,views):
    item=obj({'result':enum(['pass','fail','uncertain']),'confidence':{'type':'number','minimum':0,'maximum':1},
        'views':{'type':'array','items':enum(views),'uniqueItems':True,'minItems':1,'maxItems':len(views)},
        'evidence':{'type':'string','minLength':1,'maxLength':500},'uncertainty_reason':{'type':'string','maxLength':400}})
    return obj({'criteria':obj({r:item for r in RULES[kind]}),'summary':{'type':'string','minLength':1,'maxLength':600}})

def visual_decision(value):
    items=list(value['criteria'].values())
    if any(i['result']=='uncertain' and not i['uncertainty_reason'].strip() for i in items):raise ValueError('Uncertainty requires an explicit reason')
    if any(i['result']=='fail' and i['confidence']>=.75 for i in items):return 'FAIL'
    return 'PASS' if all(i['result']=='pass' and i['confidence']>=.75 for i in items) else 'NeedsReview'

def merge_decisions(deterministic,visual):
    return 'FAIL' if 'FAIL' in (deterministic,visual) else 'PASS' if deterministic==visual=='PASS' else 'NeedsReview'

def outputs_for(db,job):return {s['name']:db.artifact_dict(s['id']) for s in db.stages(job) if s['status']=='Completed'}

def gate_sources(kind,outputs):
    requested=[('ReferenceGenerated','reference'),('ReferenceProcessed','preprocessed')] if kind=='reference' else [('GeometryGenerated','geometry')]
    result={}
    for stage,key in requested:
        try:info=outputs[stage][key]
        except KeyError:raise ProductionError('GateArtifactMissing',stage+'/'+key+' required; no inference submitted')
        if not intact(info):raise ProductionError('GateArtifactIntegrity','Gate source changed: '+info['path'])
        result[key]=info
    return result

def current_gate(db,store,job_id,kind):
    report=store.gate_report(job_id,kind)
    if not report:return None
    try:
        asset=json.loads(db.job(job_id)['request_json']);sources=gate_sources(kind,outputs_for(db,job_id))
        if report.get('sources')!=sources or report.get('source_sha256')!=digest(asset) or report.get('profile',{}).get('config_sha256')!=profile_for(asset)['config_sha256'] or report.get('version')!=VERSION:return None
        from .quality_config import load_quality
        config=load_quality()
        if report.get('model')!=config['model'] or report.get('model_digest')!=config.get('model_digests',{}).get(config['model']):return None
        for info in report.get('report_artifacts',[]):
            if not intact(info):return None
        request=json.loads((Path(report['work_dir'])/'gate-vision-request-1.json').read_text('utf-8'))
        if request.get('model')!=config['model'] or any(request.get('options',{}).get(k)!=config[k] for k in ('num_ctx','temperature','seed')):return None
        return report
    except (ProductionError,KeyError,OSError,ValueError):return None

def gate_summary(db,store,job_id):
    result={kind:current_gate(db,store,job_id,kind) for kind in ('reference','geometry')}
    return {'status':merge_decisions(*(result[k]['status'] if result[k] else 'NeedsReview' for k in ('reference','geometry'))),
        'reference':{'report_id':result['reference']['report_id'],'status':result['reference']['status']} if result['reference'] else {'status':'NeedsReview','reason':'Current reference gate missing/stale'},
        'geometry':{'report_id':result['geometry']['report_id'],'status':result['geometry']['status']} if result['geometry'] else {'status':'NeedsReview','reason':'Current geometry gate missing/stale'}}

def apply_gate_policy(report,summary):
    report['quality_gates']=summary;report['automatic_acceptance_allowed']=False
    if summary['status']!='PASS':
        report['decision']=report['visual_status']='NeedsReview'
        report.setdefault('review_reasons',[]).append('Independent quality gates '+summary['status']+'; visual score cannot override identity/reference evidence')
    return report

def visual_cache_current(report,model,config):
    if not report.get('scores') or report.get('source_model')!=model or not model or not intact(model) or report.get('evaluation_protocol',{}).get('profile')!=config:return False
    renders=[a for a in report.get('render_artifacts',[]) if a.get('mode')=='material']
    if len(renders)!=6 or {a.get('view') for a in renders}!=set(VIEWS):return False
    return all(intact({k:a[k] for k in ('path','size','sha256')}) and intact(a.get('vlm_input',{})) for a in renders)

class QualityGateService:
    def __init__(self,db,store,vlm=None,lease_factory=GPUQualityLease,profile_resolver=None):
        self.db=db;self.store=store;self.vlm=vlm or OllamaVLM();self.lease_factory=lease_factory
        self.profile_resolver=profile_resolver or profile_for
    def run(self,kind,job_id,outputs=None,parent_lock=None,refresh=False):
        if kind not in RULES:raise ValueError('Unknown gate kind')
        job=self.db.job(job_id);asset=json.loads(job['request_json']);settings=json.loads(job['settings_json']);profile=self.profile_resolver(asset)
        outputs=outputs if outputs is not None else outputs_for(self.db,job_id)
        ident=uuid.uuid4().hex;work=self.store.output_root/'Gates'/job_id/kind/ident
        work.mkdir(parents=True,exist_ok=False);started=time.monotonic();sources={};key=None
        report={'report_id':ident,'job_id':job_id,'kind':kind,'version':VERSION,'profile':profile,'source_sha256':digest(asset),
                'created_at':utcnow(),'status':'NeedsReview','mode':'shadow','production_status_unchanged':job['status'],
                'human_verified':False,'confidence_is_calibrated_probability':False,'sources':sources,'work_dir':str(work)}
        try:
            sources=gate_sources(kind,outputs);report['sources']=sources
            key=digest({'version':VERSION,'profile':profile,'asset':asset,'sources':sources,'model':self.vlm.model,'digest':self.vlm.digest,'protocol':self.vlm.profile})
            if not refresh:
                row=self.store.db.execute('SELECT report_json FROM QualityGateReports WHERE request_key=? ORDER BY created_at DESC LIMIT 1',(key,)).fetchone()
                if row:
                    prior=json.loads(row[0])
                    if not prior.get('error') and all(intact(i) for i in prior.get('report_artifacts',[])):
                        # Empty UUID directory is harmless; no old reports overwritten.
                        return {**prior,'cache_reused':True}
            if kind=='reference':
                deterministic=reference_analysis(sources['reference']['path'],sources['preprocessed']['path'],profile)
                original=work/'reference.png';shutil.copyfile(sources['reference']['path'],original)
                with Image.open(sources['preprocessed']['path']) as im:
                    rgba=im.convert('RGBA');white=Image.new('RGBA',rgba.size,'white');white.alpha_composite(rgba);white.convert('RGB').save(work/'foreground.png');rgba.getchannel('A').save(work/'mask.png')
                images=[original,work/'foreground.png',work/'mask.png'];views=['reference','foreground','mask']
                instruction='Compare the original reference, foreground composited on white, and alpha mask. Detect cropped closeups, isolated wall/roof pieces, lost object parts after removal, wrong foreground, off-center framing. White/black mask indicates selection, not a real object.'
            else:
                deterministic=geometry_analysis(sources['geometry']['path'],profile,work)
                command=[settings['blender'],'--background','--python-exit-code','23','--python',AI/'Scripts/blender_preview.py','--',sources['geometry']['path'],
                    '--quality-output',work/'renders','--resolution','512','--samples','12']
                run_process(command,work,settings['subprocess_timeout'],label='identity-render')
                meta=json.loads((work/'renders/render-metadata.json').read_text('utf-8'))
                if meta['source_sha256']!=sources['geometry']['sha256']:raise ProductionError('RenderIntegrity','Identity render source mismatch')
                images=[work/'renders'/(v+'.png') for v in VIEWS];views=VIEWS
                instruction='Inspect untextured raw Shape geometry views. Judge whole object identity and structural completeness, not color or texture. Thin walls, isolated roof fragments, flattened or broken pieces are not complete buildings. Thin fences, towers, trees and debris must be judged by their own class.'
                report['render_metadata']=meta
            report['deterministic']=deterministic;atomic_json(work/'deterministic.json',deterministic)
            metadata=self.vlm.metadata();atomic_json(work/'model-metadata.json',metadata)
            with self.lease_factory(self.vlm,work,comfy_url=settings['comfy_url'],minimum_free_mib=self.vlm.profile['minimum_free_mib'],parent_lock=parent_lock) as lease:
                lease.mark_owned()
                prompt=(instruction+' Expected class: '+profile['object_class']+'. Source prompt is a requested design, NOT observed evidence. '
                    'Cite only supplied image IDs: '+','.join(views)+'. Explicit uncertainty_reason for uncertain criteria; do not invent hidden parts. '
                    'A numeric confidence is an uncalibrated model opinion. Return brief structured evidence. Source data: '+json.dumps({'prompt':asset['prompt'],'category':asset.get('category')},ensure_ascii=False))
                value,metrics=self.vlm.structured(prompt,gate_schema(kind,views),work,'gate-vision',images=images,num_predict=2200,validate_value=visual_decision)
                placement=lease.placement()
            visual=visual_decision(value)
            if not placement.get('verified') or placement.get('cpu_offload_detected'):visual='NeedsReview'
            report.update(status=merge_decisions(deterministic['status'],visual),vlm={'status':visual,'criteria':value['criteria'],'summary':value['summary']},
                model=self.vlm.model,model_digest=metadata['installed']['digest'],performance={'vlm':metrics},model_placement=placement)
        except Exception as exc:
            report['error']={'category':getattr(exc,'category',type(exc).__name__),'message':str(exc)}
            # Never erase a deterministic failure just because the VLM failed.
            report['status']='FAIL' if report.get('deterministic',{}).get('status')=='FAIL' else 'NeedsReview'
        report['elapsed_seconds']=time.monotonic()-started
        report['suggestions']=(['Use a farther camera and complete-object framing.','Simplify prompt around the whole structure.','Remove complex background; review the foreground mask against the original.','Consider a new reference seed only after explicit production authorization.'] if kind=='reference' else
            ['Review/change the reference image before another Shape run.','Consider a new Shape seed or reviewed resolution/steps.','Choose a workflow emphasizing whole-object structure.','Do not run PBR or create a new candidate until identity passes.']) if report['status']!='PASS' else []
        report['report_artifacts']=[file_info(p) for p in work.rglob('*') if p.is_file()]
        atomic_json(work/'gate-report.json',report)
        self.store.db.execute('INSERT INTO QualityGateReports VALUES (?,?,?,?,?,?,?,?)',(ident,job_id,kind,report['status'],key,report['created_at'],str(work),canonical(report)));self.store.db.commit()
        return report

    def full_check(self,job_id,refresh=False):
        reference=self.run('reference',job_id,refresh=refresh);geometry=self.run('geometry',job_id,refresh=refresh)
        previous=self.store.gate_report(job_id,'full')
        evaluation=None
        if previous and not refresh:
            cached=json.loads(self.store.get(previous['visual_evaluation_id'])['report_json'] or '{}')
            from .quality_config import load_quality
            model=outputs_for(self.db,job_id).get('Scaled',{}).get('model')
            if visual_cache_current(cached,model,load_quality()):
                evaluation=cached
                if previous.get('version')==VERSION and previous['reference_report_id']==reference['report_id'] and previous['geometry_report_id']==geometry['report_id']:
                    return {**previous,'cache_reused':True}
        # Regression inspection deliberately evaluates final appearance even when a
        # gate fails; it submits no Shape/PBR and cannot approve or resume a job.
        from .quality_service import QualityService
        reused=evaluation is not None
        evaluation=evaluation or QualityService(self.db,self.store).evaluate(job_id)
        gates=gate_summary(self.db,self.store,job_id)
        status='FAIL' if gates['status']=='FAIL' or evaluation.get('technical_status')=='TechnicalFailed' else 'NeedsReview'
        result={'report_id':uuid.uuid4().hex,'job_id':job_id,'kind':'full','version':VERSION,'status':status,'gate_status':gates['status'],
            'reference_report_id':reference['report_id'],'geometry_report_id':geometry['report_id'],'visual_evaluation_id':evaluation['evaluation_id'],
            'visual_scores':evaluation.get('scores'),'overall_score':evaluation.get('overall_score'),'visual_decision':evaluation['decision'],
            'technical_status':evaluation.get('technical_status'),'visual_evaluation_reused':reused,
            'visual_evaluation_error':evaluation.get('error'),'no_production_inference':True,'human_approval':False,'created_at':utcnow()}
        work=self.store.output_root/'Gates'/job_id/'full'/result['report_id'];work.mkdir(parents=True,exist_ok=False)
        atomic_json(work/'full-check.json',result)
        self.store.db.execute('INSERT INTO QualityGateReports VALUES (?,?,?,?,?,?,?,?)',(result['report_id'],job_id,'full',result['status'],None,result['created_at'],str(work),canonical(result)));self.store.db.commit();return result
