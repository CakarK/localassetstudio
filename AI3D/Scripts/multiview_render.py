"""Blender-only renderer. Reads the final GLB, writes a fresh quality directory."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

VIEWS={'front':(0,-1,.4),'back':(0,1,.4),'left':(-1,0,.4),'right':(1,0,.4),
       'isometric_45':(1,-1,math.sqrt(2)),'top':(0,0,1)}


def emission(name,color):
    material=bpy.data.materials.new(name);material.use_nodes=True
    nodes=material.node_tree.nodes;nodes.clear();out=nodes.new('ShaderNodeOutputMaterial');shader=nodes.new('ShaderNodeEmission')
    shader.inputs['Color'].default_value=(*color,1);material.node_tree.links.new(shader.outputs[0],out.inputs['Surface'])
    return material,shader


def main(argv):
    p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('--quality-output',required=True)
    p.add_argument('--resolution',type=int,default=768);p.add_argument('--samples',type=int,default=24)
    p.add_argument('--diagnostics',nargs='*',choices=['silhouette','wireframe','normal','base_color'],default=[])
    p.add_argument('--views',nargs='+',choices=list(VIEWS),default=list(VIEWS))
    args=p.parse_args(argv);src=Path(args.input).resolve();out=Path(args.quality_output).resolve()
    if args.resolution<256 or args.samples<1:raise ValueError('Invalid render settings')
    out.mkdir(parents=True,exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True);bpy.ops.import_scene.gltf(filepath=str(src))
    meshes=[o for o in bpy.context.scene.objects if o.type=='MESH']
    points=[o.matrix_world@Vector(c) for o in meshes for c in o.bound_box]
    if not points or not all(math.isfinite(x) for pt in points for x in pt):raise RuntimeError('No finite mesh bounds')
    lo=Vector([min(pt[i] for pt in points) for i in range(3)]);hi=Vector([max(pt[i] for pt in points) for i in range(3)])
    center=(lo+hi)/2;extent=max(hi-lo);radius=max((pt-center).length for pt in points)
    if extent<=0:raise RuntimeError('Empty mesh')
    scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=args.samples;scene.cycles.seed=42
    scene.render.resolution_x=args.resolution;scene.render.resolution_y=args.resolution;scene.render.resolution_percentage=100
    scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA';scene.render.film_transparent=False
    scene.world=bpy.data.worlds.new('QualityWorld');scene.world.use_nodes=True
    scene.world.node_tree.nodes['Background'].inputs[0].default_value=(.7,.7,.7,1)
    scene.world.node_tree.nodes['Background'].inputs[1].default_value=.5
    for name,pos,power,size in [('Key',(2,-3,4),700,4),('Fill',(-3,-1,2),450,3),('Rim',(1,3,4),850,3)]:
        bpy.ops.object.light_add(type='AREA',location=center+Vector(pos)*extent)
        light=bpy.context.object;light.name=name;light.data.energy=power*extent**2;light.data.size=size*extent
        light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
    bpy.ops.object.camera_add();camera=bpy.context.object;scene.camera=camera;camera.data.lens=50
    camera.data.clip_start=max(.0001,extent*.001);camera.data.clip_end=extent*100
    original_materials={o.name:list(o.data.materials) for o in meshes};entries=[]
    modes=['material',*args.diagnostics]
    for mode in modes:
        modifiers=[]
        if mode in ('silhouette','wireframe','normal'):
            material,shader=emission('Quality_'+mode,(.01,.01,.01))
            if mode=='normal':
                nodes=material.node_tree.nodes;geometry=nodes.new('ShaderNodeNewGeometry');scale=nodes.new('ShaderNodeVectorMath');scale.operation='SCALE';scale.inputs[3].default_value=.5
                add=nodes.new('ShaderNodeVectorMath');add.operation='ADD';add.inputs[1].default_value=(.5,.5,.5)
                links=material.node_tree.links;links.new(geometry.outputs['Normal'],scale.inputs[0]);links.new(scale.outputs[0],add.inputs[0]);links.new(add.outputs[0],shader.inputs['Color'])
            scene.view_layers[0].material_override=material
            if mode=='wireframe':
                for obj in meshes:
                    mod=obj.modifiers.new('QualityWireframe','WIREFRAME');mod.thickness=extent*.001;modifiers.append((obj,mod))
        elif mode=='base_color':
            for obj in meshes:
                for i,original in enumerate(original_materials[obj.name]):
                    material=original.copy();obj.data.materials[i]=material
                    if not material.use_nodes:continue
                    nodes=material.node_tree.nodes;principled=next((n for n in nodes if n.type=='BSDF_PRINCIPLED'),None)
                    output=next((n for n in nodes if n.type=='OUTPUT_MATERIAL'),None)
                    if not principled or not output:continue
                    shader=nodes.new('ShaderNodeEmission');base=principled.inputs['Base Color'];shader.inputs['Color'].default_value=base.default_value
                    if base.links:material.node_tree.links.new(base.links[0].from_socket,shader.inputs['Color'])
                    material.node_tree.links.new(shader.outputs[0],output.inputs['Surface'])
        for view in args.views:
            direction=Vector(VIEWS[view]).normalized();rotation=(-direction).to_track_quat('-Z','Y')
            camera.rotation_euler=rotation.to_euler()
            ortho=view in ('isometric_45','top');camera.data.type='ORTHO' if ortho else 'PERSP'
            distance=radius/max(math.sin(camera.data.angle/2),.1)*1.25
            camera.location=center+direction*distance
            local=[rotation.inverted()@(pt-center) for pt in points]
            camera.data.ortho_scale=max(max(pt[i] for pt in local)-min(pt[i] for pt in local) for i in (0,1))*1.2
            scene.render.filepath=str(out/(view+('' if mode=='material' else '_'+mode)+'.png'))
            bpy.ops.render.render(write_still=True)
            path=Path(scene.render.filepath)
            projected=[world_to_camera_view(scene,camera,point) for point in points]
            frame_bounds=[[min(p[i] for p in projected),max(p[i] for p in projected)] for i in (0,1)]
            in_frame=all(0<=low<=high<=1 for low,high in frame_bounds) and all(p.z>0 for p in projected)
            entries.append({'view':view,'mode':mode,'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                            'camera_type':camera.data.type,'position':list(camera.location),'rotation_euler':list(camera.rotation_euler),
                            'ortho_scale':camera.data.ortho_scale,'lens_mm':camera.data.lens,'distance':distance,
                            'projected_bounds_ndc':frame_bounds,'all_bounding_corners_in_frame':in_frame})
        scene.view_layers[0].material_override=None
        for obj,mod in modifiers:obj.modifiers.remove(mod)
        for obj in meshes:
            obj.data.materials.clear()
            for material in original_materials[obj.name]:obj.data.materials.append(material)
    report={'source':str(src),'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'blender_version':bpy.app.version_string,
            'bounds_blender_m':[list(lo),list(hi)],'triangles':sum(len(p.vertices)-2 for o in meshes for p in o.data.polygons),
            'render':{'engine':'CYCLES','device':'CPU','resolution':args.resolution,'samples':args.samples,'seed':42},
            'axis_note':'Blender Z-up; front/back labels are world directions, not inferred architectural facade identity.',
            'artifacts':entries}
    (out/'render-metadata.json').write_text(json.dumps(report,indent=2),'utf-8')
    print('QUALITY_RENDER_COMPLETE',len(entries))
