import json
import numpy as np
from pathlib import Path
from PIL import Image
import trimesh
from .common import file_info

def validate(model,settings,files,blender_report):
    checks=[]
    def add(name,status,detail):checks.append({'check':name,'status':status,'detail':detail})
    try:
        mesh=trimesh.load(model,force='mesh',process=False)
        add('mesh_load','PASS' if len(mesh.vertices) and len(mesh.faces) else 'FAIL',{'vertices':len(mesh.vertices),'triangles':len(mesh.faces)})
        add('finite_vertices','PASS' if np.isfinite(mesh.vertices).all() else 'FAIL','All vertex coordinates must be finite.')
        add('triangle_budget','PASS' if 0<len(mesh.faces)<=settings['polygon_target'] else 'FAIL',{'actual':len(mesh.faces),'limit':settings['polygon_target']})
        lower,upper=mesh.bounds;size=upper-lower
        limit=settings.get('footprint_m')
        add('footprint','PASS' if limit and size[0]<=limit[0]+1e-4 and size[2]<=limit[1]+1e-4 else 'FAIL' if limit else 'WARN',
            {'width_height_depth_m':size.tolist(),'limit_width_depth_m':limit,'reason':'Unknown source footprint is not claimed as verified.' if not limit else None})
        height=settings.get('height_m')
        add('height','PASS' if height and abs(size[1]-height)<=max(1e-4,height*.001) else 'FAIL' if height else 'WARN',{'actual':float(size[1]),'target':height})
        pivot_ok=abs(lower[1])<1e-4 and abs(lower[0]+upper[0])<1e-4 and abs(lower[2]+upper[2])<1e-4
        add('base_center_pivot','PASS' if pivot_ok else 'FAIL',{'bounds':mesh.bounds.tolist()})
        uv=getattr(mesh.visual,'uv',None)
        add('uv_present','PASS' if uv is not None and len(uv)==len(mesh.vertices) else 'FAIL','Require UVs for every textured vertex.')
        add('uv_finite_and_range','PASS' if uv is not None and np.isfinite(uv).all() and (uv>=-1e-5).all() and (uv<=1.00001).all() else 'FAIL','UV coordinates must be finite and inside 0..1.')
        material=getattr(mesh.visual,'material',None)
        pbr=material is not None and getattr(material,'baseColorTexture',None) is not None and getattr(material,'metallicRoughnessTexture',None) is not None
        add('pbr_material_bindings','PASS' if pbr else 'FAIL','GLB base color and metallic-roughness texture bindings.')
    except Exception as exc:add('mesh_load','FAIL',str(exc))
    for name in ('albedo','metallic','roughness'):
        try:
            with Image.open(files[name]) as im:
                size=im.size;im.verify()
            add('texture_'+name,'PASS' if size==(settings['texture_size'],)*2 else 'FAIL',{'size':size,'expected':settings['texture_size']})
        except Exception as exc:add('texture_'+name,'FAIL',str(exc))
    try:
        report=json.loads(Path(blender_report).read_text('utf-8'))
        source_ok=report.get('source_sha256')==file_info(model)['sha256']
        add('blender_source_provenance','PASS' if source_ok else 'FAIL','Blender report must describe these exact GLB bytes.')
        add('fbx_dimensions_roundtrip','PASS' if report.get('fbx_scale_roundtrip_pass') and report.get('fbx_dimensions_roundtrip_pass') else 'FAIL',report.get('fbx_dimensions'))
        meshes=report.get('meshes',[])
        add('normals_finite','PASS' if meshes and all(m.get('normals_finite') for m in meshes) else 'FAIL','Blender recalculated finite face normals.')
        lods=report.get('lods',[])
        add('lod_reimport','PASS' if len(lods)==2 and all(0<x.get('triangles',0)<=report.get('base_triangles',0) for x in lods) else 'FAIL',lods)
    except Exception as exc:add('blender_report','FAIL',str(exc))
    for name in ('fbx','obj','blend','lod1','lod2'):
        path=Path(files.get(name,''))
        valid=path.is_file() and path.stat().st_size>32
        if valid and name.startswith('lod') or valid and name=='fbx':
            valid=path.read_bytes()[:18]==b'Kaydara FBX Binary'
        add('export_'+name,'PASS' if valid else 'FAIL',str(path))
    for name in ('preview_front','preview_back'):
        try:
            with Image.open(files[name]) as im:
                im.load()
                nonempty=im.width>=256 and im.height>=256 and ('A' not in im.getbands() or im.getchannel('A').getextrema()[1]>0)
                dimensions=im.size
            with Image.open(files[name]) as im:im.verify()
            add(name,'PASS' if nonempty else 'FAIL',{'size':dimensions,'nonempty':nonempty})
        except Exception as exc:add(name,'FAIL',str(exc))
    for name in ('semantic_prompt_adherence','artistic_quality','quad_retopology','uv_overlap_and_lightmap','real_unreal_import'):
        add(name,'WARN','Not verified in this phase; no PASS is claimed.')
    status='FAIL' if any(c['status']=='FAIL' for c in checks) else 'WARN' if any(c['status']=='WARN' for c in checks) else 'PASS'
    return {'status':status,'checks':checks,'counts':{s:sum(c['status']==s for c in checks) for s in ('PASS','WARN','FAIL')}}
