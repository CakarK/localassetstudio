"""Blender 5 headless validation + FBX/OBJ export. Operates on generated files only."""
import argparse
import json
import math
import hashlib
import sys
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector

def dimensions(objects):
    points=[o.matrix_world @ Vector(corner) for o in objects for corner in o.bound_box]
    if not points:raise RuntimeError('No mesh bounds')
    return [max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)]

def triangle_count(objects):
    return sum(len(poly.vertices)-2 for obj in objects for poly in obj.data.polygons)

p=argparse.ArgumentParser()
p.add_argument('input');p.add_argument('--lods',action='store_true')
args=p.parse_args(sys.argv[sys.argv.index('--')+1:])
src=Path(args.input).resolve();out=src.parent
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(src))
meshes=[o for o in bpy.context.scene.objects if o.type=='MESH']
if not meshes:raise RuntimeError('No meshes imported')
reports=[]
for obj in meshes:
    bm=bmesh.new();bm.from_mesh(obj.data)
    bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
    bm.normal_update()
    audit=bm.copy()
    bmesh.ops.remove_doubles(audit,verts=list(audit.verts),dist=1e-6)
    welded_non_manifold=sum(not e.is_manifold for e in audit.edges)
    audit.free()
    uv=[loop.uv[:] for loop in obj.data.uv_layers.active.data] if obj.data.uv_layers.active else []
    reports.append({'name':obj.name,'vertices':len(bm.verts),'faces':len(bm.faces),
                    'non_manifold_edges':sum(not e.is_manifold for e in bm.edges),
                    'non_manifold_edges_after_weld_audit':welded_non_manifold,
                    'uv_finite':all(math.isfinite(x) for co in uv for x in co),
                    'uv_outside_0_1':sum(any(x < -1e-5 or x > 1.00001 for x in co) for co in uv),
                    'uv_layers':len(obj.data.uv_layers),'materials':len(obj.data.materials),
                    'normals_finite':all(math.isfinite(x) for f in bm.faces for x in f.normal),
                    'dimensions_m':list(obj.dimensions),'finite_coordinates':all(math.isfinite(x) for v in bm.verts for x in v.co)})
    bm.to_mesh(obj.data);bm.free()
    obj.select_set(True)
bpy.context.scene.unit_settings.system='METRIC'
bpy.context.scene.unit_settings.scale_length=1.0
bpy.ops.wm.save_as_mainfile(filepath=str(out/'validated.blend'))
bpy.ops.export_scene.fbx(filepath=str(out/'unreal.fbx'),use_selection=True,object_types={'MESH'},
    apply_unit_scale=True,apply_scale_options='FBX_SCALE_UNITS',axis_forward='-Z',axis_up='Y',
    use_mesh_modifiers=True,add_leaf_bones=False,path_mode='COPY',embed_textures=True)
bpy.ops.wm.obj_export(filepath=str(out/'unreal.obj'),export_selected_objects=True,forward_axis='NEGATIVE_Z',up_axis='Y')
if args.lods:
    for level,ratio in [(1,0.5),(2,0.25)]:
        for obj in meshes:
            modifier=obj.modifiers.new('AI3D_LOD','DECIMATE');modifier.ratio=ratio
        bpy.ops.export_scene.fbx(filepath=str(out/f'unreal_LOD{level}.fbx'),use_selection=True,object_types={'MESH'},
            apply_unit_scale=True,apply_scale_options='FBX_SCALE_UNITS',axis_forward='-Z',axis_up='Y',add_leaf_bones=False)
        for obj in meshes:obj.modifiers.remove(obj.modifiers['AI3D_LOD'])
# Reimport the actual exported FBX and compare dimensions in Blender's meters.
expected_dimensions=dimensions(meshes);base_triangles=triangle_count(meshes)
expected=max(expected_dimensions)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=str(out/'unreal.fbx'))
reimported=[o for o in bpy.context.scene.objects if o.type=='MESH']
actual_dimensions=dimensions(reimported);actual=max(actual_dimensions)
scale_ok=abs(actual-expected)<=max(0.0001,expected*0.001)
dimensions_ok=all(abs(a-b)<=max(0.0001,a*0.001) for a,b in zip(expected_dimensions,actual_dimensions))
lod_reports=[]
if args.lods:
    for level in (1,2):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.fbx(filepath=str(out/f'unreal_LOD{level}.fbx'))
        imported=[o for o in bpy.context.scene.objects if o.type=='MESH']
        lod_reports.append({'level':level,'triangles':triangle_count(imported),'dimensions_m':dimensions(imported)})
result={'input':str(src),'blender_version':bpy.app.version_string,'meshes':reports,
        'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'base_triangles':base_triangles,'lods':lod_reports,
        'fbx_dimensions':{'expected_m':expected_dimensions,'actual_m':actual_dimensions},'fbx_dimensions_roundtrip_pass':dimensions_ok,
        'fbx_reimport_max_dimension_m':actual,'original_max_dimension_m':expected,'fbx_scale_roundtrip_pass':scale_ok,
        'unreal_expected_max_dimension_cm':actual*100,
        'note':'Automatic cleanup/decimation is not manual retopology; inspect topology, seams and collision before shipping.'}
(out/'blender-validation.json').write_text(json.dumps(result,indent=2),'utf-8')
if not scale_ok or not dimensions_ok:raise RuntimeError('FBX scale roundtrip failed')
print('BLENDER_VALIDATION_PASS',json.dumps(result))
