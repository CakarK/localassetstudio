"""Small checkpoint adapters; existing Hunyuan and LocalAssetStudio nodes remain in use."""
import gc
import os
import re
import uuid
from pathlib import Path
import folder_paths

class SaveMesh:
    @classmethod
    def INPUT_TYPES(cls):return {'required':{'mesh':('TRIMESH',),'namespace':('STRING',),'filename':('STRING',{'default':'geometry.glb'})}}
    RETURN_TYPES=('STRING',);FUNCTION='run';OUTPUT_NODE=True;CATEGORY='Game3DCreator/Managed'
    def run(self,mesh,namespace,filename):
        if not re.fullmatch(r'GM_[a-f0-9_]+',namespace):raise ValueError('Invalid managed namespace')
        if filename not in ('geometry.glb','pbr.glb'):raise ValueError('Invalid managed artifact name')
        directory=Path(folder_paths.get_output_directory())/namespace;directory.mkdir(parents=True,exist_ok=True)
        path=directory/filename;temporary=directory/(uuid.uuid4().hex+'.glb')
        mesh.export(temporary);os.replace(temporary,path)
        import comfy.model_management as mm
        import torch
        mm.unload_all_models();gc.collect();mm.soft_empty_cache();torch.cuda.empty_cache()
        return {'ui':{'managed_artifacts':[str(path)]},'result':(str(path),)}

class LoadMesh:
    @classmethod
    def INPUT_TYPES(cls):return {'required':{'path':('STRING',)}}
    RETURN_TYPES=('TRIMESH',);FUNCTION='run';CATEGORY='Game3DCreator/Managed'
    def run(self,path):
        import trimesh
        resolved=Path(path).resolve();root=Path(folder_paths.get_output_directory()).resolve()
        if not resolved.is_relative_to(root):raise ValueError('Managed mesh must be inside the configured output directory')
        return (trimesh.load(resolved,force='mesh',process=False),)

NODE_CLASS_MAPPINGS={'G3DCManagedSaveMesh':SaveMesh,'G3DCManagedLoadMesh':LoadMesh}
