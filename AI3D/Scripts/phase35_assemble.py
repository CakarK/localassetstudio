"""Only run with a prevalidated reviewed assembly spec; never synthesizes accessories."""
import bpy,json,sys,math
from pathlib import Path
spec_path,output=sys.argv[sys.argv.index('--')+1:]
spec=json.loads(Path(spec_path).read_text('utf-8'));output=Path(output);output.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
body_root=bpy.data.objects.new('ParentBody',None);bpy.context.collection.objects.link(body_root)
bpy.ops.import_scene.gltf(filepath=spec['parent_asset']['path'])
for obj in list(bpy.context.scene.objects):
    if obj!=body_root and obj.parent is None:obj.parent=body_root
before=set(bpy.context.scene.objects)
bpy.ops.import_scene.gltf(filepath=spec['accessory_asset']['path'])
mount=bpy.data.objects.new('AccessoryAttachment',None);bpy.context.collection.objects.link(mount);mount.parent=body_root
for obj in set(bpy.context.scene.objects)-before-{mount}:
    if obj.parent is None:obj.parent=mount
t=spec['relative_transform'];mount.location=t['translation_m'];mount.rotation_euler=[math.radians(x) for x in t['rotation_degrees']];mount.scale=t['scale']
mount['attachment_metadata']=json.dumps(spec,ensure_ascii=False);mount['collision_relationship']=spec['collision_relationship']
bpy.ops.wm.save_as_mainfile(filepath=str(output/'assembly.blend'))
bpy.ops.export_scene.gltf(filepath=str(output/'assembly.glb'),export_format='GLB',export_yup=True)
(output/'assembly-report.json').write_text(json.dumps({'source_parent':spec['parent_asset'],'source_accessory':spec['accessory_asset'],
    'objects':[o.name for o in bpy.context.scene.objects],'separate_child_component':True,'ai_generation_claim':False,
    'collision_relationship_metadata_only':True,'automatic_approval':False},indent=2),'utf-8')
