import json
import math
import shutil
import time
from pathlib import Path
from PIL import Image
from .common import AI, ProductionError, atomic_json, digest, file_info, intact, utcnow
from .processes import run_process
from .quality_requirements import requirements
from .quality_schema import EXTRACTION, response_schema
from .quality_schema import parse_response
from .quality_scoring import policy, score
from .quality_vlm import OllamaVLM, GPUQualityLease
from .criteria_cache import CriteriaCache
from .quality_compact import compact_schema,expand,prompt_for,merge_batches,PROMPT_VERSION,grounding_schema,validate_grounding,apply_grounding

VIEWS=['front','back','left','right','isometric_45','top']
EVALUATION_VERSION='visual-shadow-1.0'

def validate_config(config):
    allowed={'resolution','diagnostics','ai_extraction','allow_cpu_fallback','scoring','render_timeout','vlm_timeout','quality'}
    if not isinstance(config,dict) or set(config)-allowed:raise ValueError('Unknown/invalid visual evaluation config')
    if type(config.get('resolution',768)) is not int or not 256<=config.get('resolution',768)<=2048:raise ValueError('resolution must be an integer in 256..2048')
    for key in ('ai_extraction','allow_cpu_fallback'):
        if key in config and type(config[key]) is not bool:raise ValueError(key+' must be boolean')
    for key in ('render_timeout','vlm_timeout'):
        if key in config and (not isinstance(config[key],(int,float)) or not math.isfinite(config[key]) or config[key]<=0):raise ValueError('Invalid '+key)
    diagnostics=config.get('diagnostics',[])
    if not isinstance(diagnostics,list) or any(v not in ('silhouette','wireframe','normal','base_color') for v in diagnostics) or len(diagnostics)!=len(set(diagnostics)):raise ValueError('Invalid diagnostic modes')
    policy(config.get('scoring'))
    if 'quality' in config:
        from .quality_config import load_quality
        load_quality(config['quality'])
    return config

def production_artifacts(db,job_id):
    job=db.job(job_id);outputs={s['name']:db.artifact_dict(s['id']) for s in db.stages(job_id) if s['status']=='Completed'}
    if 'Scaled' not in outputs or 'Validated' not in outputs:raise ProductionError('TechnicalValidationMissing','Final mesh and technical validation checkpoints required')
    model=outputs['Scaled']['model'];technical=outputs['Validated']['validation']
    if not intact(model) or not intact(technical):raise ProductionError('ArtifactIntegrity','Production model or technical report hash changed')
    return job,outputs,json.loads(Path(technical['path']).read_text('utf-8'))

def map_renders(directory,model_info):
    directory=Path(directory).resolve();metadata=json.loads((directory/'render-metadata.json').read_text('utf-8'))
    if metadata['source_sha256']!=model_info['sha256']:raise ProductionError('RenderIntegrity','Renders do not describe the final mesh bytes')
    artifacts=[]
    for item in metadata['artifacts']:
        path=(directory/Path(item['path']).name).resolve()
        if not path.is_relative_to(directory):raise ProductionError('RenderIntegrity','Invalid render path')
        info=file_info(path)
        if info['sha256']!=item['sha256']:raise ProductionError('RenderIntegrity','Render image hash changed')
        with Image.open(path) as im:im.verify()
        with Image.open(path) as im:width,height=im.size
        artifacts.append({**item,**info,'width':width,'height':height})
    material=[a for a in artifacts if a['mode']=='material']
    if len(material)!=6 or {a['view'] for a in material}!=set(VIEWS):raise ProductionError('RenderIntegrity','Six uniquely mapped mesh views required')
    return metadata,artifacts

def reusable_render(store,job_id,model,resolution=768,diagnostics=None,exclude=None):
    job_rows=[r for r in store.db.execute('SELECT * FROM QualityEvaluations WHERE job_id=? ORDER BY created_at DESC',(job_id,))]
    for previous in job_rows:
        if previous['id']==exclude:continue
        try:
            meta,_=map_renders(Path(previous['work_dir'])/'renders',model)
            if meta['render']['resolution']==resolution and set(diagnostics or [])<={a['mode'] for a in meta['artifacts']}:return previous['id']
        except (OSError,ValueError,KeyError,ProductionError):continue
    return None

class QualityService:
    def __init__(self,db,store,vlm=None,lease_factory=GPUQualityLease,fallback_invalid_extraction=False):
        self.db=db;self.store=store;self.vlm=vlm or OllamaVLM();self.custom_vlm=vlm is not None;self.lease_factory=lease_factory
        self.fallback_invalid_extraction=fallback_invalid_extraction
    def evaluate(self,job_id,config=None,reuse_evaluation=None,prepared_evaluation=None):
        config=validate_config(config or {});scoring_policy=policy(config.get('scoring'));job,outputs,technical=production_artifacts(self.db,job_id)
        if config.get('quality') and not self.custom_vlm:self.vlm=OllamaVLM(config=config['quality'])
        if config.get('vlm_timeout'):self.vlm.timeout=config['vlm_timeout']
        evaluation=prepared_evaluation or self.store.begin(job,config)
        if evaluation['job_id']!=job_id or evaluation['status']!='Running':raise ValueError('Invalid pre-reserved visual evaluation')
        ident=evaluation['id'];work=Path(evaluation['work_dir']);started=time.monotonic()
        try:
            model=outputs['Scaled']['model'];asset=json.loads(job['request_json']);settings=json.loads(job['settings_json'])
            atomic_json(work/'source-snapshot.json',{'job':job,'model':model,'technical_report':outputs['Validated']['validation'],'outputs':outputs})
            print('Quality',ident[:8],'rendering final mesh',flush=True)
            render_started=time.monotonic()
            if not reuse_evaluation:
                reuse_evaluation=reusable_render(self.store,job_id,model,config.get('resolution',768),config.get('diagnostics'),ident)
            if reuse_evaluation:
                previous=self.store.get(reuse_evaluation);source=Path(previous['work_dir'])/'renders'
                map_renders(source,model);shutil.copytree(source,work/'renders')
            else:
                command=[settings['blender'],'--background','--python-exit-code','23','--python',AI/'Scripts/blender_preview.py','--',model['path'],
                         '--quality-output',work/'renders','--resolution',str(config.get('resolution',768))]
                if config.get('diagnostics'):command+=['--diagnostics',*config['diagnostics']]
                run_process(command,work,config.get('render_timeout',300),label='quality-render')
            render_elapsed=time.monotonic()-render_started
            metadata,artifacts=map_renders(work/'renders',model)
            material=[a for a in artifacts if a['mode']=='material'];inputs=work/'vlm-inputs';inputs.mkdir()
            images=[]
            for item in material:
                with Image.open(item['path']) as im:
                    im=im.convert('RGB');im.thumbnail((512,512));path=inputs/(item['view']+'.png');im.save(path)
                    item['vlm_width'],item['vlm_height']=im.size;item['vlm_input']=file_info(path);images.append(path)
            atomic_json(work/'render-artifacts.json',artifacts)
            base=requirements(asset);atomic_json(work/'requirements-base.json',base)
            criteria_started=time.monotonic();cache=CriteriaCache()
            interpretation,cache_status=cache.find_existing(asset,self.store) if config.get('ai_extraction',True) else (base,'template_only')
            criteria_elapsed=time.monotonic()-criteria_started
            if min(a['vlm_width'] for a in material)<512:
                raise ProductionError('InsufficientRenderResolution','Minimum 512px VLM input required; rerender/detail review needed')
            print('Quality',ident[:8],'checking local VLM and GPU resources',flush=True)
            model_metadata=self.vlm.metadata();atomic_json(work/'model-metadata.json',model_metadata)
            lease=self.lease_factory(self.vlm,work,comfy_url=settings['comfy_url'],allow_cpu=config.get('allow_cpu_fallback',False),
                                     minimum_free_mib=self.vlm.profile['minimum_free_mib'])
            with lease:
                lease.mark_owned()
                ai=None;extraction_metrics=None
                if interpretation is None and config.get('ai_extraction',True):
                    print('Quality',ident[:8],'extracting source-anchored requirements',flush=True)
                    prompt=('Extract at most 12 atomic asset requirements. Exact quote must be a literal substring of the supplied field, including any newline. '
                            'Use visual for observable exterior features, technical for dimensions, unverifiable for capacity or historical facts. Do not invent requirements. Source data:\n'+
                            json.dumps({k:asset[k] for k in ('prompt','shared_style','shared_negative')},ensure_ascii=False))
                    try:
                        ai,extraction_metrics=self.vlm.structured(prompt,EXTRACTION,work,'requirements',num_predict=2400,validate_value=lambda value:requirements(asset,value))
                    except ProductionError as exc:
                        if exc.category!='VLMInvalidResponse' or not self.fallback_invalid_extraction:raise
                        # AI extraction is optional enrichment. Invalid quotes
                        # must never enter the criteria cache, but do not prevent
                        # actual mesh evaluation against the literal source template.
                        interpretation=base;cache_status='template_after_invalid_ai_extraction'
                        extraction_metrics={'error':{'category':exc.category,'message':str(exc)},'fallback':'source_anchored_template'}
                        atomic_json(work/'requirements-fallback.json',extraction_metrics)
                    else:
                        interpretation=cache.put(asset,requirements(asset,ai),{'evaluation_id':ident})
                        criteria_elapsed+=extraction_metrics['elapsed_seconds']
                interpretation=interpretation or base;atomic_json(work/'requirements.json',interpretation)
                lease.queue_empty()
                print('Quality',ident[:8],'evaluating six real mesh views',flush=True)
                strategy=self.vlm.profile['image_strategy'];groups=[list(range(6))] if strategy=='six' else [list(range(3)),list(range(3,6))]
                direct=self.vlm.profile.get('protocol')=='qwen-evidence-3.2'
                prompt_version='qwen-mesh-evidence-3.2' if direct else PROMPT_VERSION
                payloads=[];calls=[];facts=[];grounding_calls=[]
                for number,group in enumerate(groups):
                    lease.queue_empty();view_ids=[material[n]['view'] for n in group];schema=compact_schema(interpretation['requirements'],view_ids)
                    if direct:
                        prompt=('Inspect the actual final 3D mesh images in order: '+','.join(view_ids)+'. The requested prompt is NOT visible proof. '
                            'Judge each supplied visual criterion with evidence views and confidence 0..1 (uncalibrated opinion). '
                            'For uncertain/not_observable, evidence MUST explain why it cannot be judged; request detail views for tiny/hidden accessories. '
                            'Explicitly check whole object identity against the supplied source; a detached part is not a complete object. '
                            'Check source materials, requested details, unwanted platforms, distortions and whole-object silhouette. '
                            'Never infer capacity, physical dimensions or hidden interiors from pixels. Absence needs two supporting views. '
                            'Do not give high geometry/readability scores to a fragment just because surfaces are smooth. Source and criteria data:\n'+
                            json.dumps({'prompt':asset['prompt'],'style':asset['shared_style'],'negative':asset['shared_negative'],
                                        'criteria':[{k:r[k] for k in ('id','description','importance')} for r in interpretation['requirements'] if r['visually_evaluable']]},ensure_ascii=False,separators=(',',':')))
                        value,measure=self.vlm.structured(prompt,response_schema(interpretation['requirements'],view_ids),work,'vision-'+str(number+1),
                            images=[images[n] for n in group])
                        payloads.append(value);calls.append(measure);continue
                    if self.vlm.profile.get('grounding',True):
                        fact,ground_metrics=self.vlm.structured('Inspect ONLY these images in order: '+','.join(view_ids)+
                            '. No expected design is provided. Describe what is actually visible. '
                            'Classify whole object form: a complete enclosed building, a thin building fragment/panel/roof, natural object, vehicle, prop or uncertain. '
                            'Roof material must reflect pixels. Is there a raised platform extending beyond the walls? '
                            'not_seen means not observed, not proven absent. Do not invent hidden accessories. confidence is 0,0.25,0.5,0.75 or 1.',
                            grounding_schema(view_ids),work,'grounding-'+str(number+1),images=[images[n] for n in group],
                            validate_value=lambda value,view_ids=view_ids:validate_grounding(value,view_ids))
                        facts.extend(fact['views']);grounding_calls.append(ground_metrics)
                    prompt=prompt_for(asset,interpretation['requirements'],view_ids)+'\nIndependent image-only observations (not source requirements):'+json.dumps(facts,separators=(',',':'))
                    compact,measure=self.vlm.structured(prompt,schema,work,'vision-'+str(number+1),images=[images[n] for n in group],
                        validate_value=lambda value,view_ids=view_ids:expand(value,interpretation['requirements'],view_ids))
                    payloads.append(expand(compact,interpretation['requirements'],view_ids));calls.append(measure)
                payload=merge_batches(payloads)
                grounding_report=None
                if facts:payload,grounding_report=apply_grounding(payload,facts,interpretation['requirements'])
                payload['contradictions']=payload['contradictions'][:20]
                payload=parse_response({'done':True,'done_reason':'stop','message':{'content':json.dumps(payload)}},response_schema(interpretation['requirements'],VIEWS))
                all_calls=grounding_calls+calls
                metrics={'elapsed_seconds':sum(c['elapsed_seconds'] for c in all_calls),'load_seconds':sum(c['load_seconds'] for c in all_calls),
                         'prompt_eval_count':sum(c['prompt_eval_count'] or 0 for c in all_calls),'eval_count':sum(c['eval_count'] or 0 for c in all_calls),
                         'calls':calls,'grounding_calls':grounding_calls,'request_count':len(all_calls),'all_six_views_covered':True,'image_strategy':strategy}
                atomic_json(work/'expanded-vlm-payload.json',payload)
                placement=lease.placement()
            report=score(payload,interpretation,technical,material,scoring_policy)
            if report['decision']=='VisuallyApprovedCandidate':
                report['decision']=report['visual_status']='NeedsReview';report['review_reasons'].append('Provider is uncalibrated; explicit human approval required')
            if not placement.get('verified') or placement.get('cpu_offload_detected'):
                report['decision']=report['visual_status']='NeedsReview';report['review_reasons'].append('GPU placement unverified or CPU offload detected; inspect performance report')
            report.update(asset_id=asset['asset_id'],job_id=job_id,evaluation_id=ident,model=self.vlm.model,
                model_version=model_metadata['installed']['digest'],evaluation_version=EVALUATION_VERSION,
                criteria_version=interpretation['version'],interpretation_sha256=interpretation['interpretation_sha256'],
                prompt_version=prompt_version,evaluation_protocol={'prompt_version':prompt_version,'strategy':strategy,'profile':self.vlm.profile},
                independent_visual_evidence=grounding_report,
                protocol_sha256=digest({'prompt':prompt_version,'strategy':strategy,'num_ctx':self.vlm.profile['num_ctx'],
                                       'output_tokens':self.vlm.profile['max_output_tokens'],'criteria':interpretation['interpretation_sha256']}),
                source_model=model,technical_report=outputs['Validated']['validation'],render_artifacts=artifacts,
                model_placement=placement,render_metadata=metadata,
                performance={'render_seconds':render_elapsed,'render_cache_evaluation_id':reuse_evaluation,'criteria_seconds':criteria_elapsed,
                             'criteria_cache':cache_status,'vlm':metrics,'requirement_extraction':extraction_metrics,'total_seconds':time.monotonic()-started},
                created_at=utcnow(),production_job_status_unchanged=job['status'],human_calibration_asset_count=0)
            for item in report['requirements']:
                if item.get('visually_evaluable'):item['uncertainty_reason']=item.get('evidence','') if item['result'] in ('uncertain','not_observable') else ''
            from .quality_gates import gate_summary,apply_gate_policy
            apply_gate_policy(report,gate_summary(self.db,self.store,job_id))
            self.store.finish(ident,report);return report
        except Exception as exc:
            error={'category':getattr(exc,'category',type(exc).__name__),'message':str(exc)}
            self.store.fail(ident,error)
            failure={'evaluation_id':ident,'job_id':job_id,'asset_id':job['asset_id'],'mode':'shadow','decision':'NeedsReview',
                     'technical_status':'TechnicalPassed' if technical.get('status') in ('PASS','WARN') else 'TechnicalFailed',
                     'scores':None,'overall_score':None,'findings':[],'error':error,'evaluation_version':EVALUATION_VERSION,
                     'production_job_status_unchanged':job['status'],'elapsed_seconds':time.monotonic()-started}
            atomic_json(work/'visual-validation.json',failure);return failure
