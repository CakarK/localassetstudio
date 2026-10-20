"""Source-constrained prompt compiler. No VLM opinions become new game facts."""
import json
import re
import uuid
from pathlib import Path
from .common import atomic_json,canonical,digest,utcnow
from .quality_requirements import requirements
from .quality_planner import plan_corrections

VERSION='source-refinement-generic-1'

def normalized(text):return ' '.join(text.split()).casefold()

def compile_refinement(asset,report):
    interpretation=requirements(asset);original=asset['prompt'];preserved=[]
    prompt=original;style=asset['shared_style'];negative=asset['shared_negative']
    preserved=[{'source_quote':original,'output_phrase':original,'status':'verbatim'}]
    if len(prompt.split())>130 or len(prompt+style)>1800:raise ValueError('Refined prompt too long; no inference started')
    plan=plan_corrections(report)
    return {'version':VERSION,'asset_id':asset['asset_id'],'parent_job_id':report['job_id'],'source_evaluation_id':report['evaluation_id'],
        'original_prompt':original,'original_style':asset['shared_style'],'original_negative':asset['shared_negative'],
        'refined_prompt':prompt,'style':style,'negative':negative,'preserved_requirements':preserved,
        'source_interpretation_sha256':interpretation['interpretation_sha256'],
        'targeted_defects':[{'criterion_id':f['criterion_id'],'observation':f['problem'],'confidence':f['confidence'],'evidence_images':f['evidence_images']} for f in report.get('findings',[])],
        'changes':['Preserve the user-authored prompt, style, dimensions and materials verbatim.'],
        'planner':plan,'period':asset.get('period'),'grid':asset.get('grid'),'polygon_target':asset.get('polygon_target'),
        'negative_applied_to':['DreamShaper CLIP negative conditioning'],'no_direct_hunyuan_text_conditioning':True,
        'human_verified':False,'inference_started':False}

def refine(db,store,job_id):
    job=db.job(job_id);asset=json.loads(job['request_json']);report=store.report(job_id)
    # Stable across repeated evaluate calls: retain the first refinement for this
    # source/compiler version rather than generating extra candidates from new IDs.
    key=digest({'parent':job_id,'source':asset['prompt'],'style':asset['shared_style'],'negative':asset['shared_negative'],'version':VERSION})
    prior=store.db.execute('SELECT report_json FROM QualityRefinements WHERE request_key=?',(key,)).fetchone()
    if prior:return json.loads(prior[0])
    # Retain historical reported defects as hypotheses even if a smaller VLM misses
    # them. Their source IDs remain explicit; none become human-confirmed facts.
    combined={f['criterion_id']:{**f,'source_evaluation_id':report['evaluation_id']} for f in report.get('findings',[])}
    for row in store.evaluations(asset['asset_id']):
        if row['job_id']!=job_id or not row['report_json']:continue
        previous=json.loads(row['report_json'])
        for f in previous.get('findings',[]):combined.setdefault(f['criterion_id'],{**f,'source_evaluation_id':row['id']})
    report={**report,'findings':list(combined.values())}
    result=compile_refinement(asset,report);ident=uuid.uuid4().hex;result.update(refinement_id=ident,request_key=key,created_at=utcnow())
    store.db.execute('INSERT INTO QualityRefinements VALUES (?,?,?,?,?)',(ident,key,job_id,canonical(result),utcnow()));store.db.commit()
    work=store.output_root/'Refinements'/ident;work.mkdir(parents=True,exist_ok=False);atomic_json(work/'refinement.json',result)
    return result
