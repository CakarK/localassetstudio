"""Isolated stage runner: Shape persists latent; PBR only runs after worker gates."""
import argparse
import json
import time
from pathlib import Path

# Reuse audited runtime environment, offline checkpoints and restricted-package check.
from pilot import ROOT, pipeline_config, forbidden_check, atomic
import numpy as np
import torch
import trimesh
from PIL import Image
import sys
sys.path.insert(0,str(ROOT.parent.parent))
from ai3d_manager.trellis_axes import native_to_gltf,gltf_to_native


def mask(source, out):
    import os
    from torchvision import transforms
    from transformers import AutoModelForImageSegmentation
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    image = Image.open(source).convert('RGB')
    model = AutoModelForImageSegmentation.from_pretrained(str(ROOT/'models/ZhengPeng7__BiRefNet'),
        trust_remote_code=True, local_files_only=True).eval().cpu()
    transform = transforms.Compose([transforms.Resize((1024,1024)), transforms.ToTensor(),
        transforms.Normalize([.485,.456,.406],[.229,.224,.225])])
    with torch.inference_mode():
        alpha = transforms.ToPILImage()(model(transform(image).unsqueeze(0))[-1].sigmoid()[0,0].cpu()).resize(image.size, Image.Resampling.LANCZOS)
    coverage = float((np.asarray(alpha)>128).mean())
    if not .01 < coverage < .98:
        raise ValueError('Unreadable BiRefNet foreground mask')
    image = image.convert('RGBA'); image.putalpha(alpha); image.save(out/'preprocessed.png')
    atomic(out/'segmentation.json', {'model':'ZhengPeng7/BiRefNet','license':'MIT','device':'CPU',
        'mask_coverage':coverage,'semantic_approval':'human decision; no automatic visual gate'})


def generate(args):
    from trellis2.pipelines import Trellis2ImageTo3DPipeline
    from trellis2.modules.sparse import SparseTensor
    from o_voxel import postprocess
    forbidden_check()
    root = pipeline_config()  # verifies pinned model hashes before loading
    image = Image.open(args.reference)
    if image.mode != 'RGBA' or image.getextrema()[-1] == (255,255):
        raise ValueError('A readable transparent RGBA mask is required')
    pipe = Trellis2ImageTo3DPipeline.from_pretrained(str(root), config_file='pipeline-local-commercial.json')
    pipe.cuda(); torch.manual_seed(args.seed)
    image = pipe.preprocess_image(image)
    with torch.inference_mode():
        cond = pipe.get_cond([image],512)
        if args.stage == 'GeometryGenerated':
            coords = pipe.sample_sparse_structure(cond,32,1,{'steps':args.steps})
            shape = pipe.sample_shape_slat(cond,pipe.models['shape_slat_flow_model_512'],coords,{'steps':args.steps})
            np.savez(args.out/'shape-latent.npz',feats=shape.feats.float().cpu().numpy(),coords=shape.coords.cpu().numpy())
            meshes, _ = pipe.decode_shape_slat(shape,512)
            mesh = meshes[0]; mesh.fill_holes()
            trimesh.Trimesh(native_to_gltf(mesh.vertices.cpu().numpy()),mesh.faces.cpu().numpy(),process=False).export(args.out/'geometry.glb')
        else:
            latent = np.load(args.latent,allow_pickle=False)
            shape = SparseTensor(feats=torch.from_numpy(latent['feats']).cuda(),coords=torch.from_numpy(latent['coords']).cuda())
            tex = pipe.sample_tex_slat(cond,pipe.models['tex_slat_flow_model_512'],shape,{'steps':args.steps})
            mesh = pipe.decode_latent(shape,tex,512)[0]
            np.savez(args.out/'material-volume.npz',coords=mesh.coords.cpu().numpy(),attrs=mesh.attrs.float().cpu().numpy())
            bake(args,mesh.coords,mesh.attrs,mesh.vertices,mesh.faces)


def bake(args,coords,attrs,source_vertices,source_faces):
    from o_voxel import postprocess
    optimized=trimesh.load(args.optimized,force='mesh',process=False)
    result=postprocess.to_glb(vertices=torch.tensor(gltf_to_native(optimized.vertices),dtype=torch.float32,device='cuda'),
        faces=torch.tensor(optimized.faces,dtype=torch.int32,device='cuda'),attr_volume=attrs.float(),
        coords=coords.int(),attr_layout={'base_color':slice(0,3),'metallic':slice(3,4),'roughness':slice(4,5),'alpha':slice(5,6)},
        voxel_size=1/512,aabb=[[-.5]*3,[.5]*3],decimation_target=len(optimized.faces),texture_size=args.texture_size,
        remesh=False,verbose=False,bake_source_vertices=source_vertices,bake_source_faces=source_faces.int())
    result.export(args.out/'pbr.glb');material=result.visual.material
    material.baseColorTexture.save(args.out/'albedo.png')
    packed=np.asarray(material.metallicRoughnessTexture)
    Image.fromarray(packed[:,:,1]).save(args.out/'roughness.png')
    Image.fromarray(packed[:,:,2]).save(args.out/'metallic.png')


def bake_cached(args):
    forbidden_check()
    data=np.load(args.volume,allow_pickle=False)
    source=trimesh.load(args.geometry,force='mesh',process=False)
    bake(args,torch.from_numpy(data['coords']).cuda(),torch.from_numpy(data['attrs']).cuda(),
        torch.tensor(gltf_to_native(source.vertices),dtype=torch.float32,device='cuda'),
        torch.tensor(source.faces,dtype=torch.int32,device='cuda'))
    atomic(args.out/'repair.json',{'new_shape_inference':False,'new_pbr_inference':False,
        'method':'Original high-resolution surface BVH projection for cached PBR volume',
        'measured_seconds':time.monotonic()-start})


def optimize(args):
    import cumesh
    forbidden_check()
    source=trimesh.load(args.geometry,force='mesh',process=False)
    mesh=cumesh.CuMesh()
    mesh.init(torch.tensor(source.vertices,dtype=torch.float32,device='cuda'),torch.tensor(source.faces,dtype=torch.int32,device='cuda'))
    # Same cleanup/simplification sequence as the audited O-Voxel pilot export.
    mesh.fill_holes(max_hole_perimeter=3e-2)
    mesh.simplify(args.target*3,verbose=False)
    mesh.remove_duplicate_faces();mesh.repair_non_manifold_edges()
    mesh.remove_small_connected_components(1e-5);mesh.fill_holes(max_hole_perimeter=3e-2)
    mesh.simplify(args.target,verbose=False)
    mesh.remove_duplicate_faces();mesh.repair_non_manifold_edges()
    mesh.remove_small_connected_components(1e-5);mesh.fill_holes(max_hole_perimeter=3e-2)
    mesh.unify_face_orientations()
    if mesh.num_faces>args.target:mesh.simplify(args.target,verbose=False)
    vertices,faces=mesh.read()
    result=trimesh.Trimesh(vertices.cpu().numpy(),faces.cpu().numpy(),process=False)
    if not 0<len(result.faces)<=args.target or not np.isfinite(result.vertices).all():raise ValueError('CuMesh output fails triangle/finite checks')
    result.export(args.out/'optimized.glb')
    atomic(args.out/'optimization.json',{'method':'Audited CuMesh GPU cleanup and quadric simplification; not retopology',
        'input_faces':len(source.faces),'output_faces':len(result.faces),'target':args.target,'up_axis':'Y',
        'measured_seconds':time.monotonic()-start})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('stage',choices=['ReferenceProcessed','GeometryGenerated','GeometryOptimized','PBRGenerated','BakeCached'])
    p.add_argument('out',type=Path); p.add_argument('--reference',required=True,type=Path)
    p.add_argument('--latent',type=Path); p.add_argument('--optimized',type=Path)
    p.add_argument('--geometry',type=Path);p.add_argument('--target',type=int,default=4500)
    p.add_argument('--volume',type=Path)
    p.add_argument('--seed',type=int,default=42); p.add_argument('--steps',type=int,default=12)
    p.add_argument('--texture-size',type=int,default=1024)
    args = p.parse_args(); args.out.mkdir(parents=True,exist_ok=True); start = time.monotonic()
    if args.stage == 'ReferenceProcessed':mask(args.reference,args.out)
    elif args.stage == 'GeometryOptimized':optimize(args)
    elif args.stage == 'BakeCached':bake_cached(args)
    else:
        generate(args)
        atomic(args.out/'provider.json',{'provider':'trellis2','pipeline':'512','stage':args.stage,
            'seed':args.seed,'steps':args.steps,'measured_seconds':time.monotonic()-start,
            'geometry_up_axis':'Y','latent_and_volume_up_axis':'Z',
            'models':[{'model':m['model'],'revision':m['revision']} for m in json.loads((ROOT/'model-tree-plan.json').read_text())],
            'shape_inference':args.stage=='GeometryGenerated','pbr_inference':args.stage=='PBRGenerated',
            'normal_produced':False,'ao_produced':False,'old_mesh_reused':False})
