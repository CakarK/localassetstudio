"""One bounded 512-resolution trial using audited local models and free export."""
import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parent.parent
sys.path.insert(0,str(ROOT/'src/TRELLIS.2'))
os.environ.update(ATTN_BACKEND='sdpa',SPARSE_ATTN_BACKEND='sdpa',SPARSE_CONV_BACKEND='flex_gemm',
                  HF_HOME=str(ROOT/'cache/huggingface'),HF_MODULES_CACHE=str(ROOT/'cache/huggingface/modules'),
                  HF_HUB_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
                  FLEX_GEMM_AUTOTUNE_CACHE_PATH=str(ROOT/'cache/flex_gemm/autotune_cache.json'),
                  TRITON_CACHE_DIR=str(ROOT/'cache/triton'),TORCHINDUCTOR_CACHE_DIR=str(ROOT/'cache/inductor'))
from PIL import Image
import numpy as np
import torch
import trimesh

def atomic(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2),encoding='utf-8');temp.replace(path)

def hash_file(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def forbidden_check():
    installed={d.metadata['Name'].lower().replace('_','-') for d in importlib.metadata.distributions()}
    forbidden={'nvdiffrast','nvdiffrec-render','meshlib','briarmbg'}
    if installed&forbidden:raise RuntimeError('Restricted dependency installed: '+str(installed&forbidden))
    return {'restricted_distributions_present':sorted(installed&forbidden),'packages':sorted(installed)}

def native_smoke():
    """Synthetic cube exercises kernels/export; it is not a generated game asset."""
    from o_voxel import serialize,postprocess
    out=ROOT/'smoke';out.mkdir(exist_ok=True)
    coords=torch.tensor([[0,0,0],[1,2,3],[7,5,4]],dtype=torch.int32,device='cuda')
    torch.testing.assert_close(serialize.decode_seq(serialize.encode_seq(coords)),coords)
    source=trimesh.creation.box(extents=[.5,.5,.5])
    grid=torch.cartesian_prod(*[torch.arange(4,device='cuda')]*3).int()
    attrs=torch.tensor([.7,.2,.1,0.,.75,1.],device='cuda').repeat(len(grid),1)
    layout={'base_color':slice(0,3),'metallic':slice(3,4),'roughness':slice(4,5),'alpha':slice(5,6)}
    mesh=postprocess.to_glb(vertices=torch.tensor(source.vertices,dtype=torch.float32,device='cuda'),
        faces=torch.tensor(source.faces,dtype=torch.int32,device='cuda'),attr_volume=attrs,coords=grid,
        attr_layout=layout,voxel_size=.25,aabb=[[-.5]*3,[.5]*3],decimation_target=24,texture_size=128,
        remesh=False,verbose=False)
    mesh.export(out/'synthetic-kernel-check.glb')
    restored=trimesh.load(out/'synthetic-kernel-check.glb',force='mesh')
    assert len(restored.faces)>0 and np.isfinite(restored.vertices).all()
    material=restored.visual.material
    assert material.baseColorTexture is not None and material.metallicRoughnessTexture is not None
    color=np.asarray(material.baseColorTexture)[:,:,:3]
    assert color[:,:,0].max()>100 and color[:,:,0].mean()>color[:,:,1].mean()
    return {'status':'PASS','synthetic_only':True,'pretrained_inference':False,
            'checks':['CUDA O-Voxel serialization roundtrip','CuMesh cleanup and xatlas UV',
                      'cuBVH projection','FlexGEMM attribute sampling','CPU UV bake','PBR GLB reload'],
            'triangles':len(restored.faces),'cuda_runtime':torch.version.cuda,'gpu':torch.cuda.get_device_name(),
            'restricted_dependency_check':forbidden_check()}

def pipeline_config():
    dino=ROOT/'models/facebook__dinov3-vitl16-pretrain-lvd1689m'
    if not (ROOT/'dino-downloads.json').is_file() or not (dino/'model.safetensors').is_file():
        raise RuntimeError('DINOv3AccessPending: approve access and log in locally, then download_models.py --dino')
    for name in ['public-model-downloads.json','dino-downloads.json']:
        record=json.loads((ROOT/name).read_text())
        for f in record['files']:
            path=Path(f['path'])
            if not path.is_absolute():path=ROOT/path
            if path.stat().st_size!=f['bytes'] or hash_file(path)!=f['sha256']:
                raise RuntimeError('Model integrity failed: '+str(path))
    model_root=ROOT/'models/microsoft__TRELLIS.2-4B'
    config=json.loads((model_root/'pipeline.json').read_text())
    models=config['args']['models']
    for key in list(models):
        if key.endswith('_1024'):del models[key]
    for key,value in models.items():
        if key=='sparse_structure_decoder':
            models[key]='../microsoft__TRELLIS-image-large/ckpts/ss_dec_conv3d_16l8_fp16'
    config['args']['image_cond_model']['args']['model_name']=str(dino.resolve())
    config['args']['rembg_model']=None
    config['args']['default_pipeline_type']='512'
    config['args']['low_vram']=True
    atomic(model_root/'pipeline-local-commercial.json',config)
    return model_root
