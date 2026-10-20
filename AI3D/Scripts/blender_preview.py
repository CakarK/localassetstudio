"""Render the actual generated GLB for visual inspection."""
from pathlib import Path
import sys
import bpy
from mathutils import Vector

# Opt-in Phase 3 rendering; preserve the legacy two-preview entry path.
if '--quality-output' in sys.argv:
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from multiview_render import main
    main(sys.argv[sys.argv.index('--')+1:])
    raise SystemExit(0)

src=Path(sys.argv[sys.argv.index('--')+1]).resolve()
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(src))
objects=[o for o in bpy.context.scene.objects if o.type=='MESH']
points=[o.matrix_world@Vector(c) for o in objects for c in o.bound_box]
lo=Vector(tuple(min(p[i] for p in points) for i in range(3)))
hi=Vector(tuple(max(p[i] for p in points) for i in range(3)))
center=(lo+hi)/2
extent=max(hi-lo)
opposite='--opposite' in sys.argv
bpy.ops.object.camera_add(location=center+Vector((-2.4,3.8,2.5) if opposite else (2.4,-3.8,2.5))*extent)
camera=bpy.context.object
camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.type='ORTHO';camera.data.ortho_scale=extent*1.8
scene=bpy.context.scene;scene.camera=camera
for name,pos,power,size in [('Key',(2,-3,4),700,4),('Fill',(-3,-1,2),450,3),('Rim',(1,3,4),850,3)]:
    bpy.ops.object.light_add(type='AREA',location=center+Vector(pos)*extent)
    light=bpy.context.object;light.name=name;light.data.energy=power*extent*extent;light.data.shape='DISK';light.data.size=size*extent
    light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=24
scene.render.resolution_x=768;scene.render.resolution_y=768;scene.render.resolution_percentage=100
scene.world=bpy.data.worlds.new('AI3D Preview World')
scene.world.color=(0.2,0.2,0.2);scene.render.film_transparent=True
scene.render.image_settings.file_format='PNG'
scene.render.filepath=str(src.parent/('preview_opposite.png' if opposite else 'preview.png'))
bpy.ops.render.render(write_still=True)
