import json
import math
from pathlib import Path
import numpy as np
import requests
import trimesh
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
schemas=requests.get('http://127.0.0.1:8190/object_info',timeout=30).json()
results={}
for path in (ROOT/'Workflows').glob('*.api.json'):
    graph=json.loads(path.read_text('utf-8'))
    for nid,n in graph.items():
        schema=schemas[n['class_type']]
        required=schema['input'].get('required',{})
        assert set(required)<=set(n['inputs']),(path,nid,'missing input')
        for k,v in n['inputs'].items():
            if isinstance(v,list) and len(v)==2:
                assert v[0] in graph,(path,nid,'missing source')
                source=schemas[graph[v[0]]['class_type']]
                assert 0<=v[1]<len(source['output']),(path,nid,'invalid output slot')
    ui=json.loads(path.with_name(path.name.replace('.api.json','.json')).read_text('utf-8'))
    byid={n['id']:n for n in ui['nodes']}
    for link in ui['links']:
        assert link[0] in byid[link[1]]['outputs'][link[2]]['links']
        assert byid[link[3]]['inputs'][link[4]]['link']==link[0]
    results[path.name]={'schema_and_link_validation':'pass','nodes':len(graph)}

p=ROOT/'Outputs/asset_001'
served=requests.get('http://127.0.0.1:8190/view',params={'filename':'export.glb','subfolder':'asset_001','type':'output'},timeout=30)
served.raise_for_status()
assert served.content==(p/'export.glb').read_bytes()
results['preview_resource']={'http_status':served.status_code,'matches_export_glb':True}
for name in ['geometry.glb','geometry.obj','optimized.glb','textured.glb','export.glb','textured.obj','unreal.obj']:
    mesh=trimesh.load(p/name,force='mesh',process=False)
    assert len(mesh.vertices)>0 and len(mesh.faces)>0 and np.isfinite(mesh.vertices).all(),name
    results[name]={'load':'pass','vertices':len(mesh.vertices),'faces':len(mesh.faces)}
for name in ['textured.png','textured_metallic.png','textured_roughness.png']:
    with Image.open(p/name) as image:
        assert image.size==(1024,1024)
        image.verify()
    results[name]={'png_integrity':'pass','size':[1024,1024]}
scene=trimesh.load(p/'export.glb',force='mesh',process=False)
assert getattr(scene.visual,'uv',None) is not None
material=scene.visual.material
assert material.baseColorTexture is not None and material.metallicRoughnessTexture is not None
blender=json.loads((p/'blender-validation.json').read_text('utf-8'))
assert blender['fbx_scale_roundtrip_pass']
results['pbr_material']={'uv':True,'base_color':True,'metallic_roughness':True}
results['blender']=blender
(ROOT/'Logs/output-validation.json').write_text(json.dumps(results,indent=2),'utf-8')
print('PASS: 7 workflows; GLB/OBJ loads; 3 PBR maps; UV/material presence; Blender FBX scale')
