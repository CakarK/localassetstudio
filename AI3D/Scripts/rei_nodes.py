"""Local Asset Studio persistent stage nodes. No external inference services are used."""
import gc
import json
import logging
import re
import time
from pathlib import Path

import numpy as np
import requests
import torch
from PIL import Image
import folder_paths
import comfy.model_management as mm

LOG = logging.getLogger('AI3D')


def release():
    mm.unload_all_models()
    gc.collect()
    mm.soft_empty_cache()
    torch.cuda.empty_cache()
    LOG.info('VRAM allocated %.0f MB; reserved %.0f MB', torch.cuda.memory_allocated()/2**20, torch.cuda.memory_reserved()/2**20)


def project(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
        raise ValueError('Project name must contain only ASCII letters, numbers, _ or - (1-80).')
    p = Path(folder_paths.get_output_directory()) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def record(p, stage, values):
    f = p / 'parameters.json'
    data = json.loads(f.read_text('utf-8')) if f.exists() else {}
    data[stage] = dict(time=time.strftime('%Y-%m-%dT%H:%M:%S'), **values)
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), 'utf-8')
    tmp.replace(f)


def registry():
    import nodes
    return nodes.NODE_CLASS_MAPPINGS


class Prompt:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'text': ('STRING', {'multiline': True, 'default': ''}),
                             'translate_optimize': ('BOOLEAN', {'default': True})},
                'optional': {'project_name':('STRING',{'default':'asset_001'})}}
    RETURN_TYPES = ('STRING',)
    FUNCTION = 'run'
    CATEGORY = 'LocalAssetStudio/AI3D'

    def run(self, text, translate_optimize, project_name='asset_001'):
        result = text.strip()
        if translate_optimize:
            import sys
            helper_dir = str(Path(folder_paths.__file__).resolve().parent.parent/'Scripts')
            if helper_dir not in sys.path:sys.path.insert(0,helper_dir)
            from prompt_guard import translate_prompt
            translation_log=[]
            try:result = translate_prompt(text,log=translation_log)
            finally:record(project(project_name),'translation_attempts',{'attempts':translation_log})
        result += ', single isolated object, entire object visible, centered, three quarter view, plain white background, studio lighting, no text, no people'
        record(project(project_name),'prompt',{'original':text,'optimized':result,'local_translator':translate_optimize})
        return (result,)


class ImageCheckpoint:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'image': ('IMAGE',), 'project_name': ('STRING', {'default': 'asset_001'}),
                             'remove_background': ('BOOLEAN', {'default': True})},
                'hidden': {'prompt': 'PROMPT'}}
    RETURN_TYPES = ('IMAGE',)
    FUNCTION = 'run'
    CATEGORY = 'LocalAssetStudio/AI3D'

    def run(self, image, project_name, remove_background, prompt=None):
        p = project(project_name)
        im = Image.fromarray((image[0].cpu().numpy().clip(0,1)*255).astype(np.uint8))
        im.save(p/'reference.png')
        release()
        if remove_background:
            from rembg import remove, new_session
            im = remove(im, session=new_session('u2net', providers=['CPUExecutionProvider']))
        im.save(p/'preprocessed.png')
        # Hunyuan's IMAGE input uses RGBA when available.
        tensor = torch.from_numpy(np.array(im).astype(np.float32)/255.0).unsqueeze(0)
        record(p, 'image', {'remove_background':remove_background, 'workflow':prompt})
        return (tensor,)


class Shape:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'image': ('IMAGE',), 'project_name': ('STRING', {'default':'asset_001'}),
            'seed': ('INT', {'default':42, 'min':0, 'max':2**32-1}),
            'steps': ('INT', {'default':30, 'min':1, 'max':100}),
            'resolution': ('INT', {'default':384, 'min':128, 'max':768, 'step':8}),
            'polygon_target': ('INT', {'default':20000, 'min':500, 'max':1000000}),
            'allow_oom_retry': ('BOOLEAN', {'default':False})}, 'hidden':{'prompt':'PROMPT'}}
    RETURN_TYPES = ('TRIMESH',)
    FUNCTION = 'run'
    CATEGORY = 'LocalAssetStudio/AI3D'

    def run(self, image, project_name, seed, steps, resolution, polygon_target, allow_oom_retry, prompt=None):
        p = project(project_name)
        release()
        n = registry()
        requested = resolution
        attempt = 0
        while True:
            try:
                latents = n['Hy3D21MeshGenerator']().loadmodel('hunyuan3d-diffusion_models-v2-1-fp16.ckpt', image, steps, 5.0, seed, 'sdpa')[0]
                vae = n['Hy3D21VAELoader']().loadmodel('hunyuan3d-vae-v2-1-fp16.ckpt')[0]
                mesh = n['Hy3D21VAEDecode']().process(vae, latents, 1.01, resolution, 0.0, 4000, 'mc', True, True)[0]
                del vae, latents
                break
            except torch.cuda.OutOfMemoryError:
                record(p, 'shape_error', {'error':'CUDA OOM','resolution':resolution, 'previous_outputs_preserved':True})
                if 'vae' in locals():
                    vae.to('cpu')
                    del vae
                if 'latents' in locals():del latents
                release()
                if not allow_oom_retry or attempt or resolution<=256:
                    raise RuntimeError('CUDA OOM. Reference preserved. Close other GPU apps or explicitly enable a temporary lower-resolution retry.')
                resolution = 256
                attempt += 1
                LOG.warning('Explicitly enabled OOM retry: resolution %s -> 256, only for this run', requested)
        mesh.export(p/'geometry.glb')
        mesh.export(p/'geometry.obj')
        # Keep the raw mesh even when optimization fails.
        if len(mesh.faces)>polygon_target:
            mesh = n['Hy3D21PostprocessMesh']().process(mesh, True, True, True, polygon_target, False)[0]
        mesh.export(p/'optimized.glb')
        record(p,'shape',{'seed':seed,'steps':steps,'requested_resolution':requested,'effective_resolution':resolution,
                          'polygon_target':polygon_target,'faces':len(mesh.faces),'workflow':prompt})
        release()
        return (mesh,)


class MeshCheckpoint:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'project_name':('STRING',{'default':'asset_001'}),
                            'filename':(['optimized.glb','geometry.glb','textured.glb'],)}}
    RETURN_TYPES = ('TRIMESH',)
    FUNCTION = 'run'
    CATEGORY = 'LocalAssetStudio/AI3D'
    def run(self, project_name, filename):
        import trimesh
        return (trimesh.load(project(project_name)/filename,force='mesh'),)


class Paint:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'mesh':('TRIMESH',),'image':('IMAGE',),'project_name':('STRING',{'default':'asset_001'}),
            'texture_size':([512,1024,2048,4096],{'default':1024}),
            'view_size':([512,768,1024],{'default':512}),
            'steps':('INT',{'default':10,'min':1,'max':100}),
            'seed':('INT',{'default':42,'min':0,'max':2**32-1})},'hidden':{'prompt':'PROMPT'}}
    RETURN_TYPES = ('TRIMESH',)
    FUNCTION = 'run'
    CATEGORY = 'LocalAssetStudio/AI3D'
    def run(self,mesh,image,project_name,texture_size,view_size,steps,seed,prompt=None):
        import trimesh
        p=project(project_name)
        release()
        n=registry()
        pipeline=None
        try:
            camera=n['Hy3D21CameraConfig']().process('0, 90, 180, 270, 0, 180','0, 0, 0, 0, 90, -90','1, 0.1, 0.5, 0.1, 0.05, 0.05',1.2)[0]
            pipeline,albedo,mr,*_=n['Hy3DMultiViewsGenerator']().genmultiviews(mesh,camera,view_size,image,steps,3.0,texture_size,True,seed)
            # Diffusion finished. Baking needs the renderer, not the networks.
            pipeline.model=None
            release()
            pipeline,a,am,m,mmask,*_=n['Hy3DBakeMultiViews']().process(pipeline,camera,albedo,mr)
            a,m=pipeline.inpaint(a,am,m,mmask,True,'NS')
            pipeline.set_texture_albedo(a)
            pipeline.set_texture_mr(m)
            pipeline.save_mesh(str(p/'textured.obj'))
            out=trimesh.load(p/'textured.glb',force='mesh')
            record(p,'paint',{'texture_size':texture_size,'view_size':view_size,'steps':steps,'seed':seed,'workflow':prompt})
            return (out,)
        except Exception as exc:
            record(p,'paint_error',{'error':str(exc),'geometry_preserved':True})
            raise
        finally:
            if pipeline is not None: pipeline.model=None
            del pipeline
            release()


class Export:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'mesh':('TRIMESH',),'project_name':('STRING',{'default':'asset_001'}),
            'format':(['glb','obj'],), 'height_m':('FLOAT',{'default':1.0,'min':0.01,'max':1000.0})},
            'optional':{'output_directory':('STRING',{'default':''})}}
    RETURN_TYPES = ('STRING',)
    FUNCTION = 'run'
    OUTPUT_NODE=True
    CATEGORY='LocalAssetStudio/AI3D'
    def run(self,mesh,project_name,format,height_m,output_directory=''):
        p=project(project_name)
        mesh=mesh.copy()
        # Hunyuan/glTF use Y up and meters. Put base center at origin.
        lower,upper=mesh.bounds
        h=float(upper[1]-lower[1])
        if h<=0: raise ValueError('Zero-height mesh')
        mesh.apply_translation([-float((lower[0]+upper[0])/2),-float(lower[1]),-float((lower[2]+upper[2])/2)])
        mesh.apply_scale(height_m/h)
        dst=p/f'export.{format}'
        mesh.export(dst)
        if format!='glb': mesh.export(p/'export.glb')
        record(p,'export',{'format':format,'height_m':height_m,'units':'meters','up_axis':'Y','pivot':'base center',
                          'vertices':len(mesh.vertices),'faces':len(mesh.faces),'uv_present':getattr(mesh.visual,'uv',None) is not None})
        if output_directory.strip():
            import shutil
            target=Path(output_directory).expanduser().resolve()/project_name
            if target!=p.resolve():shutil.copytree(p,target,dirs_exist_ok=False)
        release()
        return {'ui':{'3d':[{'filename':dst.name,'subfolder':project_name,'type':'output'}]},'result':(str(dst),)}


NODE_CLASS_MAPPINGS={'LocalAssetStudioPrompt':Prompt,'LocalAssetStudioImageCheckpoint':ImageCheckpoint,'LocalAssetStudioShape':Shape,
    'LocalAssetStudioLoadCheckpoint':MeshCheckpoint,'LocalAssetStudioPBR':Paint,'LocalAssetStudioExport':Export}
NODE_DISPLAY_NAME_MAPPINGS={k:k.replace('LocalAssetStudio','Local Asset Studio · ') for k in NODE_CLASS_MAPPINGS}
