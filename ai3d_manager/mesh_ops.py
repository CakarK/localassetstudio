"""CPU mesh operations; decimation is deliberately not called retopology."""
import argparse
import json
import os
import uuid
from pathlib import Path
import numpy as np
import trimesh
from .common import atomic_json

def save(mesh,path):
    path=Path(path);temp=path.with_name(uuid.uuid4().hex+path.suffix)
    mesh.export(temp);os.replace(temp,path)

def optimize(source,destination,target):
    import pymeshlab
    out=Path(destination);out.mkdir(parents=True,exist_ok=True)
    mesh=trimesh.load(source,force='mesh',process=False)
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():raise ValueError('Empty/non-finite input geometry')
    before=len(mesh.faces)
    # Trimesh updates per-face and vertex attributes rather than serializing away UVs.
    mask=mesh.nondegenerate_faces()
    removed=int((~mask).sum());mesh.update_faces(mask);mesh.remove_unreferenced_vertices()
    ms=pymeshlab.MeshSet();ms.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices,face_matrix=mesh.faces),'geometry')
    ms.apply_filter('compute_selection_by_small_disconnected_components_per_face',nbfaceratio=0.005)
    ms.apply_filter('compute_selection_transfer_face_to_vertex',inclusive=False)
    ms.apply_filter('meshing_remove_selected_vertices_and_faces')
    ms.apply_filter('meshing_remove_null_faces')
    ms.apply_filter('meshing_remove_duplicate_faces')
    if ms.current_mesh().face_number()>target:
        ms.apply_filter('meshing_decimation_quadric_edge_collapse',targetfacenum=target,qualitythr=1.,
                        preserveboundary=True,boundaryweight=3,preservenormal=True,preservetopology=True,autoclean=True)
    current=ms.current_mesh();mesh=trimesh.Trimesh(current.vertex_matrix(),current.face_matrix(),process=False)
    mesh.update_faces(mesh.nondegenerate_faces());mesh.remove_unreferenced_vertices()
    if not len(mesh.faces) or len(mesh.faces)>target:raise ValueError('Optimization did not satisfy triangle budget')
    save(mesh,out/'optimized.glb')
    atomic_json(out/'optimization.json',{'input_faces':before,'output_faces':len(mesh.faces),'target':target,
                'degenerate_faces_removed_before_filters':removed,'method':'PyMeshLab quadric decimation; not quad retopology',
                'uv_policy':'runs before PBR unwrap; no textured UVs are serialized through PyMeshLab'})
    return out/'optimized.glb'

def scale(source,destination,footprint=None,height=None):
    mesh=trimesh.load(source,force='mesh',process=False)
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():raise ValueError('Invalid mesh')
    lower,upper=mesh.bounds;size=upper-lower
    if np.any(size<=0):raise ValueError('Zero-size mesh')
    planar=min(footprint[0]/size[0],footprint[1]/size[2]) if footprint else (height/size[1] if height else 1.)
    vertical=height/size[1] if height else planar
    mesh.apply_translation([-float((lower[0]+upper[0])/2),-float(lower[1]),-float((lower[2]+upper[2])/2)])
    mesh.apply_scale([planar,vertical,planar])
    out=Path(destination);out.mkdir(parents=True,exist_ok=True);save(mesh,out/'model.glb')
    atomic_json(out/'scale.json',{'units':'meters','up_axis':'Y','pivot':'base center','footprint_limit_m':footprint,
                'height_target_m':height,'actual_width_height_depth_m':mesh.extents.tolist(),
                'planar_factor':float(planar),'vertical_factor':float(vertical),
                'height_scaled_independently':not np.isclose(planar,vertical)})
    return out/'model.glb'

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['optimize','scale'])
    parser.add_argument('source');parser.add_argument('destination');parser.add_argument('--target',type=int,default=8000)
    parser.add_argument('--footprint');parser.add_argument('--height',type=float)
    args=parser.parse_args()
    if args.operation=='optimize':optimize(args.source,args.destination,args.target)
    else:scale(args.source,args.destination,json.loads(args.footprint) if args.footprint else None,args.height)
