import json
import shutil
import time
from pathlib import Path
import requests
from .common import AI, ProductionError, atomic_json, canonical

def classify(message,stage):
    lower=message.lower()
    if 'outofmemory' in lower or 'out of memory' in lower:return 'CUDA_OOM'
    if any(t in lower for t in ('checkpoint','model loading','no such file','filenotfound','size mismatch')):return 'ModelLoading'
    return 'PBRInference' if stage=='PBRGenerated' else 'GeometryInference' if stage=='GeometryGenerated' else 'ImageInference'

class ComfyClient:
    def __init__(self,url='http://127.0.0.1:8190',timeout=3600,poll=2,session=None):
        self.url=url.rstrip('/');self.timeout=timeout;self.poll=poll;self.session=session or requests.Session()
    def get(self,path):
        last=None
        for number in range(3):
            try:
                response=self.session.get(self.url+path,timeout=30);response.raise_for_status();return response.json()
            except requests.RequestException as exc:last=exc;time.sleep(min(number,1))
        raise ProductionError('ComfyUI_HTTP',str(last),transient=True,uncertain=True)
    def locate(self,attempt):
        pid=attempt.get('prompt_id');token=attempt['client_token']
        if pid:
            history=self.get('/history/'+pid)
            if pid in history:return 'history',pid,history[pid]
        queue=self.get('/queue')
        for item in queue.get('queue_running',[])+queue.get('queue_pending',[]):
            if item[1]==pid or item[3].get('client_id')==token:return 'queued',item[1],None
        histories=self.get('/history')
        for key,value in histories.items():
            prompt=value.get('prompt',[])
            if len(prompt)>3 and prompt[3].get('client_id')==token:return 'history',key,value
        return 'missing',pid,None
    def upload(self,image):
        try:
            with Path(image).open('rb') as stream:
                response=self.session.post(self.url+'/upload/image',files={'image':(Path(image).name,stream)},
                                           data={'type':'input','overwrite':'false'},timeout=60)
            response.raise_for_status();value=response.json()
            return '/'.join(filter(None,[value.get('subfolder'),value['name']]))
        except requests.RequestException as exc:raise ProductionError('ComfyUI_HTTP',str(exc),transient=True) from exc

    def execute(self,db,attempt,graph=None,context=None,boundary=None):
        """Never retry an uncertain POST. Recover by persisted prompt_id/client token."""
        if attempt['state'] in ('Submitting','Submitted','SubmissionUnknown','NeedsReview'):
            location,pid,history=self.locate(attempt)
            if location=='missing':
                raise ProductionError('SubmissionUnknown','No matching queue/history entry; explicit abandoned-attempt resolution required. No GPU resubmission performed.',uncertain=True)
            db.attempt_update(attempt['id'],state='Submitted',prompt_id=pid)
        else:
            # A foreign graph may already be running. Do not unload, interrupt or compete with it.
            started=time.monotonic()
            while True:
                queue=self.get('/queue')
                if not queue.get('queue_running') and not queue.get('queue_pending'):break
                if boundary:boundary()
                if time.monotonic()-started>self.timeout:raise ProductionError('ComfyUI_HTTP','Foreign queue remained busy; nothing submitted.',transient=True)
                time.sleep(self.poll)
            db.attempt_update(attempt['id'],state='Submitting',context_json=canonical(context))
            atomic_json(Path(attempt['work_dir'])/'comfy-graph.json',graph)
            try:
                response=self.session.post(self.url+'/prompt',json={'prompt':graph,'client_id':attempt['client_token'],
                    'extra_data':{'g3dc_job_id':attempt['job_id'],'g3dc_stage':attempt['stage_name'],'g3dc_attempt':attempt['id']}},timeout=60)
                if not response.ok:
                    if response.status_code>=500:
                        db.attempt_update(attempt['id'],state='SubmissionUnknown')
                        raise ProductionError('SubmissionUnknown','Server error after POST; acceptance unknown. No automatic resubmission.',uncertain=True)
                    db.attempt_update(attempt['id'],state='Rejected')
                    raise ProductionError('ComfyUI_HTTP','Graph explicitly rejected: '+response.text)
                pid=response.json()['prompt_id']
            except (requests.RequestException,ValueError,KeyError) as exc:
                db.attempt_update(attempt['id'],state='SubmissionUnknown')
                raise ProductionError('SubmissionUnknown','POST outcome unknown; not resubmitting: '+str(exc),uncertain=True) from exc
            db.attempt_update(attempt['id'],state='Submitted',prompt_id=pid)
            history=None
        started=time.monotonic()
        while True:
            if history is None:history=self.get('/history/'+pid).get(pid)
            if history:
                atomic_json(Path(attempt['work_dir'])/'comfy-history.json',history)
                if history.get('status',{}).get('status_str')!='success':
                    messages=history.get('status',{}).get('messages',[])
                    error=next((v for k,v in messages if k=='execution_error'),{})
                    message=error.get('exception_message',canonical(messages))
                    db.attempt_update(attempt['id'],state='InferenceFailed')
                    raise ProductionError(classify(message,attempt['stage_name']),message)
                return history
            # Cancellation is intentionally deferred until the owned in-flight stage completes.
            if time.monotonic()-started>self.timeout:
                raise ProductionError('SubmissionUnknown','Owned job is still unconfirmed; no cancellation/resubmission performed.',uncertain=True)
            time.sleep(self.poll)

def graphs(stage,namespace,settings,prompt=None,image_name=None,mesh_path=None):
    def node(kind,**inputs):return {'class_type':kind,'inputs':inputs}
    if stage=='ReferenceGenerated':
        return {'1':node('CheckpointLoaderSimple',ckpt_name=settings['checkpoint']),
          '2':node('CLIPTextEncode',clip=['1',1],text=prompt['optimized']),
          '3':node('CLIPTextEncode',clip=['1',1],text=prompt['negative']),
          '4':node('EmptyLatentImage',width=settings['image_size'],height=settings['image_size'],batch_size=1),
          '5':node('KSampler',model=['1',0],positive=['2',0],negative=['3',0],latent_image=['4',0],seed=settings['seed'],
                   steps=settings['image_steps'],cfg=7.,sampler_name='dpmpp_2m',scheduler='karras',denoise=1.),
          '6':node('VAEDecode',samples=['5',0],vae=['1',2]),
          '7':node('SaveImage',images=['6',0],filename_prefix=namespace+'/reference')}
    if stage=='GeometryGenerated':
        return {'1':node('Hy3D21LoadImageWithTransparency',image=image_name),
          '2':node('Hy3D21MeshGenerator',model='hunyuan3d-diffusion_models-v2-1-fp16.ckpt',image=['1',2],
                   steps=settings['shape_steps'],guidance_scale=5.,seed=settings['seed'],attention_mode='sdpa'),
          '3':node('Hy3D21VAELoader',model_name='hunyuan3d-vae-v2-1-fp16.ckpt'),
          '4':node('Hy3D21VAEDecode',vae=['3',0],latents=['2',0],box_v=1.01,octree_resolution=settings['shape_resolution'],
                   num_chunks=4000,mc_level=0.,mc_algo='mc',enable_flash_vdm=True,force_offload=True),
          '5':node('G3DCManagedSaveMesh',mesh=['4',0],namespace=namespace,filename='geometry.glb')}
    return {'1':node('Hy3D21LoadImageWithTransparency',image=image_name),
       '2':node('G3DCManagedLoadMesh',path=str(mesh_path)),
       '3':node('LocalAssetStudioPBR',mesh=['2',0],image=['1',2],project_name=namespace,texture_size=settings['texture_size'],
                view_size=settings['view_size'],steps=settings['pbr_steps'],seed=settings['seed']),
       '4':node('G3DCManagedSaveMesh',mesh=['3',0],namespace=namespace,filename='pbr.glb')}
