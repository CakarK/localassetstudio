"""Non-generative masks. Never paints RGB or infers missing anatomy."""
import importlib.util
import json
import os
import shutil
import time
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from scipy import ndimage
from .common import AI,ROOT,ProductionError,ProcessLock,atomic_json,digest,file_info,intact
from .category_pipeline import resolve_profile
from .quality_gates import QualityGateService,outputs_for
from .image_providers import DualComfyQualityLease
from .worker import Worker,settings_for
from .phase35_store import lookup,record

VERSION='segmentation-3.5.0'
SAM_PYTHON=Path('C:/ComfyUI/.venv/Scripts/python.exe')
SAM_CHECKPOINT=Path('C:/ComfyUI/models/sams/sam_vit_b_01ec64.pth')
STRATEGIES=['existing-u2net','alpha-preservation','adaptive-border','adaptive-grabcut','local-sam-vit-b']


def available_backends():
    return {'u2net':{'available':(AI/'Models/rembg/u2net.onnx').is_file(),'device':'CPU'},
        'sam':{'available':SAM_PYTHON.is_file() and SAM_CHECKPOINT.is_file() and Path('C:/ComfyUI/.venv/Lib/site-packages/segment_anything/__init__.py').is_file(),
               'weights_present':Path('C:/ComfyUI/models/sams/sam_vit_b_01ec64.pth').is_file(),
               'dependency_present_in_manager':bool(importlib.util.find_spec('segment_anything')),'runtime':str(SAM_PYTHON),'implemented_backend':True,'device':'CPU'},
        'sam2':{'dependency_present':bool(importlib.util.find_spec('sam2')),'implemented_backend':False},
        'adaptive':{'available':True,'dependencies':['OpenCV','Pillow','NumPy','SciPy'],'model_downloads':False}}


def background_evidence(rgb,organic=False):
    lab=cv2.cvtColor(rgb,cv2.COLOR_RGB2LAB).astype(np.float32)
    h,w=rgb.shape[:2];yy,xx=np.indices((h,w));x=xx/max(1,w-1)*2-1;y=yy/max(1,h-1)*2-1
    design=np.stack([np.ones_like(x),x,y,x*x,y*y,x*y],axis=-1)
    rim=(xx<5)|(xx>=w-5)|(yy<5)|(yy>=h-5);a=design[rim];border=lab[rim];keep=np.ones(len(border),bool)
    for _ in range(3):
        coefficients=np.linalg.lstsq(a[keep],border[keep],rcond=None)[0]
        residual=np.linalg.norm(border-a@coefficients,axis=1);median=np.median(residual)
        keep=residual<=max(6,median+3*np.median(abs(residual-median)))
    predicted=np.clip(design@coefficients,0,255);delta=lab-predicted
    center=np.median(border,axis=0)
    neutral_organic=organic and center[0]>180 and np.linalg.norm(center[1:]-128)<10
    weights=np.array([.15,2,2]) if neutral_organic else np.ones(3)
    distance=np.linalg.norm(delta*weights,axis=2)
    spread=np.linalg.norm((border-a@coefficients)*weights,axis=1)[keep]
    tolerance=float(np.clip(np.percentile(spread,95)+5,10,28))
    return distance,tolerance,{'border_lab_median':np.median(border,axis=0).tolist(),'border_distance_p95':float(np.percentile(spread,95)),
        'border_fit_inlier_fraction':float(keep.mean()),'background_lab_polynomial':coefficients.tolist(),'neutral_organic_chroma_mode':bool(neutral_organic),
        'tolerance':tolerance,'plain_background_supported':bool(np.percentile(spread,95)<18 and keep.mean()>=.75),
        'note':'Robust quadratic illumination fit to border pixels only; color evidence, not annotated ground truth. Unsupported textured borders require review.'}


class SegmentationStrategy:
    def __init__(self,ident):
        if ident not in STRATEGIES:raise ValueError('Unknown segmentation strategy')
        self.ident=ident

    def apply(self,source,destination,trusted_alpha=False,existing_rgba=None,organic=False):
        source=Path(source);destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
        with Image.open(source) as original:
            rgba=np.array(original.convert('RGBA'));has_alpha='A' in original.getbands()
        rgb=rgba[:,:,:3];meta={'strategy_id':self.ident,'source':file_info(source),'version':VERSION,'generated_rgb':False}
        if self.ident=='alpha-preservation':
            alpha=rgba[:,:,3]
            if not trusted_alpha or not has_alpha or not (np.any(alpha<128) and np.any(alpha>127)):
                raise ProductionError('AlphaNotTrusted','Nontrivial alpha and explicit trusted provenance required; opaque RGBA is not segmentation')
            shutil.copyfile(source,destination);meta['mode']='byte_exact_trusted_alpha'
            return meta
        if self.ident=='existing-u2net':
            if existing_rgba:
                if not intact(existing_rgba):raise ProductionError('ArtifactIntegrity','Baseline RGBA changed')
                shutil.copyfile(existing_rgba['path'],destination);meta.update(mode='copied_existing_u2net_result',baseline=existing_rgba)
                return meta
            if not (AI/'Models/rembg/u2net.onnx').is_file():raise ProductionError('BackendUnavailable','Local U2Net missing; automatic download forbidden')
            os.environ['U2NET_HOME']=str(AI/'Models/rembg')
            from rembg import remove,new_session
            result=remove(Image.fromarray(rgba),session=new_session('u2net',providers=['CPUExecutionProvider']))
            # Preserve source RGB exactly; this strategy only uses the predicted alpha.
            rgba[:,:,3]=np.asarray(result.convert('RGBA'))[:,:,3];meta['mode']='local_u2net_cpu'
        elif self.ident=='local-sam-vit-b':
            if not available_backends()['sam']['available']:raise ProductionError('BackendUnavailable','Existing isolated SAM runtime/checkpoint unavailable; no installation/download')
            from .processes import run_process
            seed=destination.parent/'sam-source-seed.png'
            SegmentationStrategy('adaptive-grabcut').apply(source,seed,organic=organic)
            run_process([SAM_PYTHON,AI/'Scripts/phase35_sam.py','--source',source,'--seed-mask',seed,'--checkpoint',SAM_CHECKPOINT,'--output',destination],destination.parent,300,label='sam-cpu')
            meta.update(mode='existing_sam_vit_b_cpu',runtime=str(SAM_PYTHON),checkpoint=file_info(SAM_CHECKPOINT),prompt=json.loads(destination.with_suffix('.sam.json').read_text('utf-8')))
            return meta
        else:
            distance,tolerance,bg=background_evidence(rgb,organic);meta['background_model']=bg
            if not bg['plain_background_supported']:raise ProductionError('SegmentationNeedsReview','Border does not support a plain-color background model')
            evidence=distance>tolerance
            if not evidence.any() or evidence.mean()>.97:raise ProductionError('SegmentationNeedsReview','No reliable foreground/background separation')
            if self.ident=='adaptive-border':
                alpha=np.clip((distance-tolerance*.7)/(tolerance*.6),0,1)*255
                # Source-colored holes remain holes. No dilation adds invented leaves.
                rgba[:,:,3]=alpha.astype(np.uint8)
            else:
                trimap=np.full(distance.shape,cv2.GC_PR_BGD,np.uint8)
                trimap[evidence]=cv2.GC_PR_FGD;trimap[distance<tolerance*.65]=cv2.GC_BGD
                trimap[distance>max(35,tolerance*2.5)]=cv2.GC_FGD
                trimap[[0,-1],:]=cv2.GC_BGD;trimap[:,[0,-1]]=cv2.GC_BGD
                if not np.any(trimap==cv2.GC_FGD):raise ProductionError('SegmentationNeedsReview','No high-confidence source foreground seed')
                cv2.setRNGSeed(42)
                cv2.grabCut(rgb,trimap,None,np.zeros((1,65)),np.zeros((1,65)),3,cv2.GC_INIT_WITH_MASK)
                rgba[:,:,3]=np.where(np.isin(trimap,[cv2.GC_FGD,cv2.GC_PR_FGD]),255,0).astype(np.uint8)
            meta['mode']='source_rgb_unchanged_cpu_extraction'
        Image.fromarray(rgba).save(destination);return meta


def mask_metrics(source,output,organic=False):
    rgb=np.array(Image.open(source).convert('RGB'));rgba=np.array(Image.open(output).convert('RGBA'))
    if rgb.shape!=rgba[:,:,:3].shape:raise ValueError('Mask dimensions differ from source')
    mask=rgba[:,:,3]>127;distance,tolerance,bg=background_evidence(rgb,organic);evidence=distance>max(22,tolerance*1.8)
    labels,n=ndimage.label(mask,np.ones((3,3)));sizes=np.bincount(labels.ravel())[1:]
    boundary=mask^ndimage.binary_erosion(mask);edges=cv2.Canny(rgb,60,140)>0;near_edge=ndimage.binary_dilation(edges,iterations=2)
    h,w=mask.shape;regions={'whole':np.ones(mask.shape,bool)}
    if organic:
        yy,xx=np.indices(mask.shape);regions.update(canopy=(yy<h*.58),trunk=(yy>=h*.50)&(yy<h*.94)&(xx>w*.28)&(xx<w*.72),branches=(yy>=h*.20)&(yy<h*.68)&(xx>w*.15)&(xx<w*.85))
    coverage={k:float((mask&evidence&roi).sum()/max(1,(evidence&roi).sum())) for k,roi in regions.items()}
    false_bg=float((mask&(distance<tolerance*.65)).sum()/max(1,mask.sum()))
    return {'foreground_occupancy':float(mask.mean()),'components':int(n),'main_component_coverage':float(sizes.max()/max(1,sizes.sum())) if len(sizes) else 0,
        'edge_preservation':float((boundary&near_edge).sum()/max(1,boundary.sum())),
        'source_color_evidence_coverage':coverage,'background_like_foreground_fraction':false_bg,
        'source_rgb_exact':bool(np.array_equal(rgb,rgba[:,:,:3])),
        'pixel_metrics_are_ground_truth':False,'region_note':'Normalized bands over source color evidence; coarse diagnostic regions, not hand-annotated anatomy or IoU ground truth',
        'background_model':bg}


def rank_candidate(row):
    g=row.get('gate') or {};m=row.get('metrics') or {};coverage=m.get('source_color_evidence_coverage',{})
    return (g.get('status')=='PASS' and row.get('status')=='PASS',
            min(coverage.values(),default=0)>=.95,-m.get('background_like_foreground_fraction',1),
            m.get('edge_preservation',0),min(coverage.values(),default=0))


class SegmentationService:
    def __init__(self,source_db,db,store,gate_factory=None):
        self.source_db=source_db;self.db=db;self.store=store
        self.gate_factory=gate_factory or (lambda d,s:QualityGateService(d,s,lease_factory=DualComfyQualityLease))

    def evaluate(self,job_id,strategies=None):
        job=self.source_db.job(job_id);asset=json.loads(job['request_json']);out=outputs_for(self.source_db,job_id)
        source=out.get('ReferenceGenerated',{}).get('reference');baseline=out.get('ReferenceProcessed',{}).get('preprocessed')
        if not source or not intact(source):raise ProductionError('ArtifactIntegrity','Intact real original PNG required')
        profile=resolve_profile(asset)
        if profile['name'] is None:raise ProductionError('CategoryNeedsReview',profile['reason'])
        selected=strategies or STRATEGIES;results=[]
        with ProcessLock(self.store.path.with_suffix('.segmentation.lock')):
            for strategy in selected:
                key=digest({'version':VERSION,'code':file_info(Path(__file__))['sha256'],'job':job_id,'source':source,'baseline':baseline,'strategy':strategy,'category':profile})
                old=lookup(self.store,'segmentation',key)
                if old:
                    if any(not intact(i) for i in old.get('artifacts',[])):raise ProductionError('ArtifactIntegrity','Saved segmentation artifacts changed; no silent regeneration')
                    results.append(old);continue
                work=self.store.output_root/'Segmentation'/job_id/key;work.mkdir(parents=True,exist_ok=True)
                r={'id':key,'parent_job_id':job_id,'asset_id':asset['asset_id'],'strategy_id':strategy,'source':source,'category_profile':profile,
                   'status':'NeedsReview','automatic_approval':False,'work_dir':str(work),'new_reference_inference':False,'new_shape_inference':False}
                started=time.monotonic()
                try:
                    rgba=work/'candidate.png';meta=SegmentationStrategy(strategy).apply(source['path'],rgba,existing_rgba=baseline if strategy=='existing-u2net' else None,organic=profile['name']=='Organic')
                    Image.open(rgba).getchannel('A').save(work/'mask.png');metrics=mask_metrics(source['path'],rgba,profile['name']=='Organic')
                    clone={k:v for k,v in asset.items() if not k.startswith(('phase33_','phase34_'))}
                    clone.update(production_role='reference_candidate',phase35_parent_job_id=job_id,phase35_gate_profile=profile['reference_gate_profile'],category_pipeline=profile,
                        quality_sidecar={'db':str(self.store.path),'root':str(self.store.output_root)})
                    if not self.db.db.execute('SELECT 1 FROM Assets WHERE asset_id=?',(clone['asset_id'],)).fetchone():self.db.sync_assets([clone])
                    settings=settings_for(clone,{'max_transient_retries':0});settings['reuse_files']={'ReferenceGenerated':{'reference':source},'ReferenceProcessed':{'preprocessed':file_info(rgba)}}
                    child,_=self.db.enqueue(clone,'seg_'+strategy.replace('-','_'),settings);child=Worker(self.db).run(child['id'])
                    if child['status']!='ReferenceReady':raise ProductionError('SegmentationJob','Reference-only replay did not stop safely')
                    gate=self.gate_factory(self.db,self.store).run('reference',child['id'])
                    status=gate['status']
                    if (strategy!='existing-u2net' and not metrics['source_rgb_exact']) or metrics['background_like_foreground_fraction']>.08:status='FAIL'
                    if profile['name']=='Organic' and min(metrics['source_color_evidence_coverage'].values())<.80 and status=='PASS':status='NeedsReview'
                    r.update(status=status,metadata=meta,metrics=metrics,gate=gate,child_job_id=child['id'],rgba=file_info(rgba),mask=file_info(work/'mask.png'))
                except Exception as e:r['error']={'category':getattr(e,'category',type(e).__name__),'message':str(e)}
                r['elapsed_seconds']=time.monotonic()-started;r['artifacts']=[file_info(p) for p in work.rglob('*') if p.is_file()]
                atomic_json(work/'segmentation-report.json',r);record(self.store,'segmentation',key,r);results.append(r)
        return self.compare(job_id)

    def compare(self,job_id):
        rows=[json.loads(r[0]) for r in self.store.db.execute("SELECT report_json FROM Phase35Records WHERE kind='segmentation'")]
        rows=[r for r in rows if r['parent_job_id']==job_id];rows.sort(key=rank_candidate,reverse=True)
        eligible=[r for r in rows if r['status']=='PASS' and (r.get('gate') or {}).get('status')=='PASS']
        result={'parent_job_id':job_id,'status':'PASS' if eligible else 'NeedsReview','selected_candidate_id':eligible[0]['id'] if eligible else None,
                'candidates':rows,'selection_uses_occupancy_only':False,'automatic_approval':False}
        atomic_json(self.store.output_root/'Segmentation'/job_id/'comparison.json',result);return result
