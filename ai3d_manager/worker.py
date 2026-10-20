import json
import os
import shutil
import sys
import time
from pathlib import Path
from .common import AI, ROOT, Halt, ProcessLock, ProductionError, atomic_json, canonical, digest, file_info, intact, utcnow
from .comfy import ComfyClient, graphs
from .processes import run_process
from .prompts import prepare
from .validation import validate

STAGES = ['PromptPrepared','ReferenceGenerated','ReferenceProcessed','ReferenceApproved','GeometryGenerated',
          'GeometryOptimized','PBRGenerated','Scaled','BlenderExported','PreviewsRendered','Validated','Packaged']
GPU_STAGES={'ReferenceGenerated','GeometryGenerated','PBRGenerated'}
DEPENDENCIES={
 'PromptPrepared':[], 'ReferenceGenerated':['PromptPrepared'], 'ReferenceProcessed':['ReferenceGenerated'],
 'ReferenceApproved':['ReferenceProcessed'],'GeometryGenerated':['ReferenceProcessed','ReferenceApproved'],
 'GeometryOptimized':['GeometryGenerated'],'PBRGenerated':['ReferenceProcessed','GeometryOptimized'],
 'Scaled':['PBRGenerated'],'BlenderExported':['Scaled'],'PreviewsRendered':['Scaled'],
 'Validated':['Scaled','PBRGenerated','BlenderExported','PreviewsRendered'],
 'Packaged':['PromptPrepared','ReferenceGenerated','ReferenceProcessed','GeometryGenerated','GeometryOptimized','PBRGenerated','Scaled','BlenderExported','PreviewsRendered','Validated']}
SETTING_KEYS={
 'PromptPrepared':['prompt','prompt_language','style','negative','variant_prompt','footprint_m'],
 'ReferenceGenerated':['seed','image_size','image_steps','checkpoint'], 'ReferenceProcessed':['remove_background'],
 'ReferenceApproved':['reference_policy','reference_approved'], 'GeometryGenerated':['seed','shape_steps','shape_resolution'],
 'GeometryOptimized':['polygon_target'],'PBRGenerated':['seed','texture_size','view_size','pbr_steps'],
 'Scaled':['footprint_m','height_m'],'BlenderExported':['blender','subprocess_timeout'],
 'PreviewsRendered':['blender','subprocess_timeout'],'Validated':['polygon_target','polygon_range','footprint_m','height_m','texture_size'],
 'Packaged':[]}
DEFAULTS={'seed':42,'image_size':512,'image_steps':25,'checkpoint':'dreamshaper_8.safetensors',
 'shape_steps':30,'shape_resolution':256,'view_size':512,'pbr_steps':10,'texture_size':1024,
 'remove_background':True,'reference_policy':'automatic_technical_only','reference_approved':False,
 'blender':'C:/Program Files/Blender Foundation/Blender 5.0/blender.exe','subprocess_timeout':300,
 'comfy_url':'http://127.0.0.1:8190','comfy_timeout':3600,'max_transient_retries':1,'allow_quality_reduction':False}
ALLOWED_SETTINGS=set(DEFAULTS)|{'prompt','prompt_language','style','negative','variant_prompt','polygon_target','polygon_range','footprint_m','height_m','image_provider','image_profile','category_profile','segmentation_strategy'}

def settings_for(asset,overrides=None):
    overrides=overrides or {}
    if set(overrides)-ALLOWED_SETTINGS:raise ValueError('Unknown setting(s): '+', '.join(set(overrides)-ALLOWED_SETTINGS))
    result={**DEFAULTS,'blender':os.environ.get('LAS_BLENDER_PATH',DEFAULTS['blender']),**{k:asset.get(k) for k in ('polygon_target','polygon_range','footprint_m','height_m','texture_size')},**overrides}
    if not isinstance(result['polygon_target'],int) or result['polygon_target']<100:raise ValueError('Invalid polygon target')
    if result['texture_size'] not in (512,1024,2048,4096):raise ValueError('Unsupported texture size')
    if result['footprint_m'] is not None and (len(result['footprint_m'])!=2 or min(result['footprint_m'])<=0):raise ValueError('Invalid footprint')
    if result['height_m'] is not None and result['height_m']<=0:raise ValueError('Invalid height')
    if result['reference_policy'] not in ('manual','automatic_technical_only'):raise ValueError('Unknown reference approval policy')
    for key in ('seed','image_size','image_steps','shape_steps','shape_resolution','view_size','pbr_steps','max_transient_retries'):
        if not isinstance(result[key],int) or result[key]<(0 if key in ('seed','max_transient_retries') else 1):raise ValueError('Invalid '+key)
    if result['image_size']%8:raise ValueError('image_size must be divisible by 8')
    if result['shape_resolution']<128 or result['shape_resolution']>768:raise ValueError('shape_resolution must be 128..768')
    if result['subprocess_timeout']<=0 or result['comfy_timeout']<=0:raise ValueError('Timeout must be positive')
    return result

def reuse_snapshot(directory):
    directory=Path(directory).resolve()
    names={'ReferenceGenerated':{'reference':'reference.png'},'ReferenceProcessed':{'preprocessed':'preprocessed.png'},
           'GeometryGenerated':{'geometry':'geometry.glb'},'GeometryOptimized':{'optimized':'optimized.glb'},
           'PBRGenerated':{'pbr':'textured.glb','albedo':'textured.png','metallic':'textured_metallic.png','roughness':'textured_roughness.png'}}
    return {stage:{key:file_info(directory/name) for key,name in files.items()} for stage,files in names.items()}

class Worker:
    def __init__(self,db,client=None,allow_gpu=False,gate_service_factory=None):
        self.db=db;self.client=client;self.allow_gpu=allow_gpu;self.results={};self.gate_service_factory=gate_service_factory

    def before_stage(self,stage,ident):
        asset=json.loads(self.db.job(ident)['request_json'])
        if asset.get('production_role')=='reference_candidate' and stage=='ReferenceApproved':
            self.db.status(ident,'ReferenceReady');raise Halt('Reference-only job; no Shape permitted.')
        if asset.get('phase34_benchmark_id') and stage=='GeometryGenerated':
            from .image_benchmark import require_fidelity
            require_fidelity(self.db,asset)
        kind='reference' if stage in ('ReferenceApproved','GeometryGenerated') else 'geometry' if stage in ('GeometryOptimized','PBRGenerated') else None
        if not kind:return
        from .quality_store import QualityStore
        from .quality_gates import QualityGateService
        primary=self.db.path==(AI/'State/asset_manager.sqlite').resolve()
        sidecar=asset.get('quality_sidecar') or asset.get('phase33_sidecar',{})
        store=QualityStore(sidecar.get('db') or (None if primary else self.db.path.with_suffix('.quality.sqlite')),sidecar.get('root') or (None if primary else self.db.output_root.parent/'quality'))
        try:
            service=(self.gate_service_factory or QualityGateService)(self.db,store)
            result=service.run(kind,ident,outputs=self.results,parent_lock=self.production_lock)
            self.db.event(ident,'QualityGateChecked',{'kind':kind,'report_id':result['report_id'],'status':result['status']})
            if result['status']!='PASS':
                code=('ReferenceGate' if kind=='reference' else 'GeometryIdentityGate')+('Failed' if result['status']=='FAIL' else 'Review')
                raise ProductionError(code,'No downstream inference allowed; review '+result.get('work_dir',result['report_id']))
        finally:store.close()

    def boundary(self,ident):
        if self.db.job(ident)['cancel_requested']:
            self.db.status(ident,'Cancelled');raise Halt('Cancellation completed at a safe stage boundary.')
        if self.db.setting('queue_paused',False):
            self.db.status(ident,'Paused');raise Halt('Queue paused at a safe stage boundary.')

    def stage_parameters(self,stage,asset,settings):
        from .image_providers import uses_provider
        params={key:settings.get(key) for key in SETTING_KEYS[stage]}
        if stage=='PromptPrepared':params['asset_prompt']={k:asset.get(k) for k in ('prompt','prompt_language','shared_style','shared_negative')}
        if stage in ('PromptPrepared','ReferenceGenerated') and uses_provider(asset,settings):
            from .image_providers import ImageGenerationProvider,provider_id
            params['image_provider']=ImageGenerationProvider(provider_id(asset,settings)).spec
            params['image_profile']=settings.get('image_profile','controlled')
        if asset.get('category_pipeline') and stage=='PromptPrepared':params['category_pipeline']=asset['category_pipeline']
        if stage=='ReferenceProcessed' and settings.get('segmentation_strategy'):
            from .segmentation import VERSION
            params.update(segmentation_strategy=settings['segmentation_strategy'],segmentation_version=VERSION,segmentation_code=file_info(ROOT/'ai3d_manager/segmentation.py')['sha256'])
        if stage in settings.get('reuse_files',{}):params['reused_source']=settings['reuse_files'][stage]
        code_files={
          'PromptPrepared':[ROOT/'ai3d_manager/prompts.py',AI/'Scripts/prompt_guard.py'],
          'ReferenceGenerated':[ROOT/'ai3d_manager/comfy.py',AI/'sources.lock.json'],
          'GeometryGenerated':[ROOT/'ai3d_manager/comfy.py',AI/'sources.lock.json',AI/'Scripts/managed_nodes.py'],
          'GeometryOptimized':[ROOT/'ai3d_manager/mesh_ops.py'], 'PBRGenerated':[ROOT/'ai3d_manager/comfy.py',AI/'sources.lock.json',AI/'Scripts/rei_nodes.py'],
          'Scaled':[ROOT/'ai3d_manager/mesh_ops.py'],'BlenderExported':[AI/'Scripts/blender_export.py'],
          'PreviewsRendered':[AI/'Scripts/blender_preview.py'],'Validated':[ROOT/'ai3d_manager/validation.py']}.get(stage,[])
        params['code']={p.name:file_info(p)['sha256'] for p in code_files}
        params['stage_protocol']=1
        return params

    def run(self,ident):
        with ProcessLock(self.db.path.with_suffix('.worker.lock')), ProcessLock(AI/'State/production.lock') as production_lock:
            self.production_lock=production_lock
            self.results={}
            job=self.db.job(ident);asset=json.loads(job['request_json']);settings=json.loads(job['settings_json'])
            if job['status']=='Cancelled':return job
            Path(job['output_dir']).mkdir(parents=True,exist_ok=True)
            try:
                request_file=Path(job['output_dir'])/'request.json'
                # Recover a crash between the enqueue DB commit and disk snapshot.
                if not request_file.exists():atomic_json(request_file,asset)
                elif json.loads(request_file.read_text('utf-8'))!=asset:
                    raise ProductionError('ValidationFailure','request.json differs from the immutable database request; refusing to package misleading metadata.')
                for stage in STAGES:
                    self.boundary(ident)
                    self.before_stage(stage,ident)
                    retries=0
                    while True:
                        params=self.stage_parameters(stage,asset,settings)
                        inputs={dep:self.results[dep] for dep in DEPENDENCIES[stage]}
                        fingerprint=digest({'stage':stage,'parameters':params,'inputs':
                            {dep:{name:info['sha256'] for name,info in artifacts.items()} for dep,artifacts in inputs.items()}})
                        cached=None
                        for row in reversed(self.db.stages(ident)):
                            if row['name']==stage and row['status']=='Completed' and row['fingerprint']==fingerprint:
                                artifacts=self.db.artifact_dict(row['id'])
                                if artifacts and all(intact(info) for info in artifacts.values()):cached=artifacts;break
                                self.db.db.execute("UPDATE JobStages SET status='Invalidated' WHERE id=?",(row['id'],));self.db.db.commit()
                                self.db.event(ident,'ArtifactIntegrityInvalidated',{'stage':stage,'stage_id':row['id']})
                        if cached:
                            self.results[stage]=cached;self.db.event(ident,'StageReused',{'stage':stage,'fingerprint':fingerprint})
                            break
                        active=next((a for a in reversed(self.db.attempts(ident)) if a['stage_name']==stage and a['state'] in ('Running','Submitting','Submitted','SubmissionUnknown','NeedsReview')),None)
                        if active:
                            old=next(s for s in self.db.stages(ident) if s['id']==active['stage_id'])
                            if stage in GPU_STAGES and active['state']!='Running' and old['fingerprint']!=fingerprint:
                                raise ProductionError('SubmissionUnknown','An earlier GPU attempt with different settings remains unresolved.',uncertain=True)
                            if stage not in GPU_STAGES or old['fingerprint']!=fingerprint:
                                if active.get('process_id'):
                                    import psutil
                                    if psutil.pid_exists(active['process_id']):
                                        try:
                                            command=psutil.Process(active['process_id']).cmdline()
                                        except psutil.Error:command=[]
                                        if any(active['work_dir'] in arg for arg in command):
                                            raise ProductionError('SubprocessUnknown','Previous owned subprocess is still active; no duplicate launched.',uncertain=True)
                                self.db.fail_stage(active,{'category':'Interrupted','message':'Previous local stage incomplete; retry in a new attempt directory.'})
                                active=None
                        attempt=active or self.db.begin_stage(ident,stage,fingerprint,params,inputs)
                        self.db.status(ident,'Running')
                        print(f"{ident[:8]} {stage} attempt {attempt['number']}",flush=True)
                        try:
                            paths=self.execute_stage(stage,attempt,asset,settings)
                            artifacts={name:file_info(path) for name,path in paths.items()}
                            if not artifacts:raise ProductionError('ExportFailure','Stage produced no artifacts.')
                            self.db.complete_stage(attempt,artifacts);self.results[stage]=artifacts
                            atomic_json(Path(attempt['work_dir'])/'stage-manifest.json',{'stage':stage,'parameters':params,'inputs':inputs,'outputs':artifacts,'result':'Completed','completed_at':utcnow()})
                            self.db.status(ident,stage)
                            break
                        except Halt:raise
                        except Exception as exc:
                            if not isinstance(exc,ProductionError):exc=ProductionError('ExportFailure',str(exc))
                            error={'category':exc.category,'message':str(exc),'transient':exc.transient,'uncertain':exc.uncertain}
                            if hasattr(exc,'prompt_record'):atomic_json(Path(attempt['work_dir'])/'rejected-prompt.json',exc.prompt_record)
                            self.db.fail_stage(attempt,error,review=exc.uncertain)
                            if exc.category=='CUDA_OOM':
                                self.clean_idle_gpu(settings)
                                if settings.get('allow_quality_reduction') and retries==0:
                                    changed={}
                                    if stage=='GeometryGenerated' and settings['shape_resolution']>256:changed={'shape_resolution':256}
                                    if stage=='PBRGenerated' and (settings['texture_size']>1024 or settings['view_size']>512):changed={'texture_size':min(settings['texture_size'],1024),'view_size':512}
                                    if changed:
                                        old={k:settings[k] for k in changed};settings.update(changed)
                                        self.db.db.execute('UPDATE GenerationJobs SET settings_json=? WHERE id=?',(canonical(settings),ident));self.db.db.commit()
                                        self.db.event(ident,'QualityReducedForOOM',{'old':old,'new':changed});retries+=1;continue
                            if exc.transient and not exc.uncertain and retries<settings['max_transient_retries']:
                                retries+=1;self.db.event(ident,'TransientRetry',error);time.sleep(.2);continue
                            self.db.status(ident,'NeedsReview' if exc.uncertain or exc.category in ('ReferenceReview','CatalogAmbiguity') else 'Failed',error)
                            return self.db.job(ident)
                self.boundary(ident)
                self.db.status(ident,'Completed')
            except Halt:pass
            except ProductionError as exc:
                review=exc.uncertain or exc.category.startswith(('ReferenceGate','GeometryIdentityGate'))
                self.db.status(ident,'NeedsReview' if review else 'Failed',{'category':exc.category,'message':str(exc)})
            return self.db.job(ident)

    def path(self,stage,name):return Path(self.results[stage][name]['path'])
    def clean_idle_gpu(self,settings):
        try:
            client=self.client or ComfyClient(settings['comfy_url']);q=client.get('/queue')
            if not q.get('queue_running') and not q.get('queue_pending'):
                response=client.session.post(client.url+'/free',json={'unload_models':True,'free_memory':True},timeout=15)
                self.db.event(None,'IdleGPUCleanup',{'http_status':response.status_code})
        except Exception:pass

    def execute_stage(self,stage,attempt,asset,settings):
        work=Path(attempt['work_dir']);ident=attempt['job_id']
        reused=settings.get('reuse_files',{}).get(stage)
        if reused:
            paths={}
            for name,info in reused.items():
                if not intact(info):raise ProductionError('ValidationFailure','Reused source changed since enqueue: '+info['path'])
                destination=work/(name+Path(info['path']).suffix)
                shutil.copyfile(info['path'],destination);paths[name]=destination
            atomic_json(work/'origin.json',{'mode':'copied_existing_real_artifacts','not_new_inference':True,'sources':reused})
            paths['origin']=work/'origin.json'
            return paths
        if stage=='PromptPrepared':
            if asset.get('category_pipeline'):
                from .category_pipeline import prompt_for
                settings={**settings,'prompt':prompt_for(asset)}
            value=prepare(asset,settings);value['existing_artifact_replay']=bool(settings.get('reuse_files'))
            from .image_providers import uses_provider
            if uses_provider(asset,settings):
                from .image_providers import ImageGenerationProvider,provider_id
                provider=ImageGenerationProvider(provider_id(asset,settings)).spec
                value.update(image_provider=provider,image_profile=settings.get('image_profile','controlled'),negative_applied_to=[provider['id']+' negative conditioning'] if provider['negative_prompt'] else [],negative_capability=provider['negative_prompt'])
            atomic_json(work/'prompt.json',value);return {'prompt':work/'prompt.json'}
        if stage in GPU_STAGES:return self.gpu_stage(stage,attempt,settings)
        if stage=='ReferenceProcessed':
            if settings.get('segmentation_strategy'):
                from .segmentation import SegmentationStrategy
                meta=SegmentationStrategy(settings['segmentation_strategy']).apply(self.path('ReferenceGenerated','reference'),work/'preprocessed.png',organic=asset.get('category_pipeline',{}).get('name')=='Organic')
                atomic_json(work/'segmentation.json',meta);return {'preprocessed':work/'preprocessed.png','segmentation':work/'segmentation.json'}
            from PIL import Image
            image=Image.open(self.path('ReferenceGenerated','reference')).convert('RGBA')
            if settings['remove_background']:
                import os
                os.environ['U2NET_HOME']=str(AI/'Models/rembg')
                from rembg import remove,new_session
                image=remove(image,session=new_session('u2net',providers=['CPUExecutionProvider']))
            image.save(work/'preprocessed.png');return {'preprocessed':work/'preprocessed.png'}
        if stage=='ReferenceApproved':
            from PIL import Image
            with Image.open(self.path('ReferenceProcessed','preprocessed')) as im:
                im.load();bbox=im.getchannel('A').getbbox() if 'A' in im.getbands() else None
                if not bbox or im.width<128 or im.height<128:raise ProductionError('ReferenceReview','Empty/invalid foreground; no 3D inference allowed.')
            if settings['reference_policy']=='manual' and not settings['reference_approved']:
                raise ProductionError('ReferenceReview','Reference awaits explicit jobs approve command.')
            atomic_json(work/'approval.json',{'approval':'technical_only','foreground_bbox':bbox,
                       'semantic_visual_approval':'not_verified','policy':settings['reference_policy']})
            return {'approval':work/'approval.json'}
        if stage=='GeometryOptimized':
            run_process([sys.executable,'-m','ai3d_manager.mesh_ops','optimize',self.path('GeometryGenerated','geometry'),work,
                         '--target',str(settings['polygon_target'])],work,settings['subprocess_timeout'],self.db,attempt,'optimize')
            return {'optimized':work/'optimized.glb','optimization_report':work/'optimization.json'}
        if stage=='Scaled':
            command=[sys.executable,'-m','ai3d_manager.mesh_ops','scale',self.path('PBRGenerated','pbr'),work]
            if settings.get('footprint_m'):command+=['--footprint',canonical(settings['footprint_m'])]
            if settings.get('height_m'):command+=['--height',str(settings['height_m'])]
            run_process(command,work,settings['subprocess_timeout'],self.db,attempt,'scale')
            return {'model':work/'model.glb','scale_report':work/'scale.json'}
        if stage=='BlenderExported':
            shutil.copyfile(self.path('Scaled','model'),work/'model.glb')
            run_process([settings['blender'],'--background','--python-exit-code','23','--python',AI/'Scripts/blender_export.py','--',work/'model.glb','--lods'],
                        work,settings['subprocess_timeout'],self.db,attempt,'blender')
            return {name:work/file for name,file in {'fbx':'unreal.fbx','obj':'unreal.obj','blend':'validated.blend',
                    'lod1':'unreal_LOD1.fbx','lod2':'unreal_LOD2.fbx','blender_report':'blender-validation.json'}.items()}
        if stage=='PreviewsRendered':
            shutil.copyfile(self.path('Scaled','model'),work/'model.glb')
            for opposite in (False,True):
                command=[settings['blender'],'--background','--python-exit-code','23','--python',AI/'Scripts/blender_preview.py','--',work/'model.glb']
                if opposite:command+=['--opposite']
                run_process(command,work,settings['subprocess_timeout'],self.db,attempt,'render-back' if opposite else 'render-front')
            return {'preview_front':work/'preview.png','preview_back':work/'preview_opposite.png'}
        if stage=='Validated':
            files={name:info['path'] for dep in ('PBRGenerated','BlenderExported','PreviewsRendered') for name,info in self.results[dep].items()}
            report=validate(self.path('Scaled','model'),settings,files,self.path('BlenderExported','blender_report'))
            atomic_json(work/'validation.json',report)
            if report['status']=='FAIL':raise ProductionError('ValidationFailure','Critical technical validation failed; see '+str(work/'validation.json'))
            return {'validation':work/'validation.json'}
        if stage=='Packaged':return self.package(attempt)
        raise ValueError('Unknown stage: '+stage)

    def gpu_stage(self,stage,attempt,settings):
        # Phase 3 may have crashed after submitting a remote VLM inference. Its
        # persistent reservation survives the OS lock and blocks GPU overlap.
        from .quality_vlm import check_vlm_reservation
        check_vlm_reservation()
        if not self.allow_gpu:raise ProductionError('GPUApprovalRequired','GPU inference requires an explicit generate/queue run invocation; replay mode can reuse existing files.')
        asset=json.loads(self.db.job(attempt['job_id'])['request_json'])
        if asset.get('phase35_parent_job_id'):
            from .quality_store import QualityStore
            from .phase35_store import claim_inference
            sidecar=asset.get('quality_sidecar',{})
            store=QualityStore(sidecar.get('db'),sidecar.get('root'))
            try:claim_inference(store,{'ReferenceGenerated':'reference','GeometryGenerated':'shape','PBRGenerated':'pbr'}[stage],attempt['id'],asset.get('phase35_asset_role'),asset.get('phase35_reference_necessity'))
            finally:store.close()
        if stage=='ReferenceGenerated':
            from .image_providers import ImageGenerationProvider,provider_id
            image_provider=provider_id(asset,settings)
            if image_provider!='dreamshaper8' or settings.get('image_provider') or asset.get('image_provider'):
                return ImageGenerationProvider(image_provider).generate(self,attempt,asset,settings)
        if asset.get('phase34_benchmark_id'):
            from .image_benchmark import guard_submission
            guard_submission(self.db,asset,attempt,'shape' if stage=='GeometryGenerated' else 'pbr')
        if asset.get('phase33_set_id'):
            from .controlled_generation import guard_gpu_submission
            guard_gpu_submission(self.db,asset,stage,attempt,settings)
        client=self.client or ComfyClient(settings['comfy_url'],settings['comfy_timeout'])
        context=json.loads(attempt.get('context_json') or '{}')
        if context:
            namespace=context['namespace'];graph=None
        else:
            namespace='GM_'+attempt['id']
            image=None;mesh=None;prompt=None
            if stage=='ReferenceGenerated':prompt=json.loads(self.path('PromptPrepared','prompt').read_text('utf-8'))
            else:image=client.upload(self.path('ReferenceProcessed','preprocessed'))
            if stage=='PBRGenerated':mesh=self.path('GeometryOptimized','optimized')
            graph=graphs(stage,namespace,settings,prompt,image,mesh)
            context={'namespace':namespace,'stage':stage}
        history=client.execute(self.db,attempt,graph,context,lambda:self.boundary(attempt['job_id']))
        directory=AI/'Outputs'/namespace;work=Path(attempt['work_dir'])
        if stage=='ReferenceGenerated':
            images=[image for result in history.get('outputs',{}).values() for image in result.get('images',[])]
            if not images:raise ProductionError('ImageInference','Successful graph returned no reference image.')
            image=images[0];source=(AI/'Outputs'/image.get('subfolder','')/image['filename']).resolve()
            if not source.is_relative_to(directory.resolve()):raise ProductionError('ExportFailure','Unexpected reference path.')
            shutil.copyfile(source,work/'reference.png');return {'reference':work/'reference.png'}
        names={'geometry':'geometry.glb'} if stage=='GeometryGenerated' else {'pbr':'pbr.glb','albedo':'textured.png','metallic':'textured_metallic.png','roughness':'textured_roughness.png'}
        result={}
        for name,filename in names.items():
            destination=work/(name+Path(filename).suffix);shutil.copyfile(directory/filename,destination);result[name]=destination
        return result

    def package(self,attempt):
        work=Path(attempt['work_dir']);staging=work/'Delivery.pending';staging.mkdir(exist_ok=False)
        mapping={'PromptPrepared':{'prompt':'prompt.json'},'ReferenceGenerated':{'reference':'reference.png'},
          'ReferenceProcessed':{'preprocessed':'preprocessed.png'},'GeometryGenerated':{'geometry':'geometry.glb'},
          'GeometryOptimized':{'optimized':'optimized.glb'},'PBRGenerated':{'pbr':'pbr.glb','albedo':'albedo.png','metallic':'metallic.png','roughness':'roughness.png'},
          'Scaled':{'model':'model.glb','scale_report':'scale.json'},
          'BlenderExported':{'fbx':'model.fbx','obj':'model.obj','blend':'model.blend','lod1':'lod1.fbx','lod2':'lod2.fbx','blender_report':'blender-validation.json'},
          'PreviewsRendered':{'preview_front':'preview_front.png','preview_back':'preview_back.png'},'Validated':{'validation':'validation.json'}}
        for stage,artifacts in mapping.items():
            for name,filename in artifacts.items():shutil.copyfile(self.path(stage,name),staging/filename)
        # OBJ may reference auxiliary material/texture files; preserve all companions.
        source=self.path('BlenderExported','obj').parent
        for path in source.rglob('*'):
            if path.is_file() and path.suffix.lower() in ('.mtl','.png','.jpg','.jpeg'):
                destination=staging/path.relative_to(source)
                if not destination.exists():
                    destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,destination)
        shutil.copyfile(Path(self.db.job(attempt['job_id'])['output_dir'])/'request.json',staging/'request.json')
        atomic_json(staging/'manifest.json',{'state':'Packaged','job_id':attempt['job_id'],
            'files':{str(p.relative_to(staging)):{k:v for k,v in file_info(p).items() if k!='path'} for p in staging.rglob('*') if p.is_file()},
            'validation':'validation.json','live_job_status':str(Path(self.db.job(attempt['job_id'])['output_dir'])/'job.json')})
        destination=work/'Delivery';os.replace(staging,destination)
        return {str(p.relative_to(destination)):p for p in destination.rglob('*') if p.is_file()}
