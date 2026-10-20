"""Bounded CPU recovery when topology-preserving decimation cannot meet budget."""
import sys
from pathlib import Path
import numpy as np
import trimesh
import pymeshlab
from .common import atomic_json,file_info
from .mesh_ops import save


def optimize(source,destination,target):
    out=Path(destination);out.mkdir(parents=True,exist_ok=True)
    mesh=trimesh.load(source,force='mesh',process=False)
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():raise ValueError('Invalid recovery source')
    before=len(mesh.faces)
    mesh.update_faces(mesh.nondegenerate_faces());mesh.remove_unreferenced_vertices()
    ms=pymeshlab.MeshSet();ms.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices,face_matrix=mesh.faces))
    for name in ('meshing_remove_duplicate_vertices','meshing_remove_null_faces','meshing_remove_duplicate_faces','meshing_remove_unreferenced_vertices'):
        ms.apply_filter(name)
    passes=[]
    # Preserve positions/normals and boundaries first. If disconnected tiny
    # open edges prevent the target, allow boundary collapse in one final pass.
    for boundary in (True,False):
        if ms.current_mesh().face_number()<=target:break
        start=ms.current_mesh().face_number()
        ms.apply_filter('meshing_decimation_quadric_edge_collapse',targetfacenum=target,qualitythr=1.,
                        preserveboundary=boundary,boundaryweight=3,preservenormal=True,preservetopology=False,autoclean=True)
        passes.append({'before':start,'after':ms.current_mesh().face_number(),'preserve_boundary':boundary,'preserve_topology':False})
    current=ms.current_mesh();result=trimesh.Trimesh(current.vertex_matrix(),current.face_matrix(),process=False)
    result.update_faces(result.nondegenerate_faces());result.remove_unreferenced_vertices()
    if not len(result.faces) or len(result.faces)>target or not np.isfinite(result.vertices).all():
        raise ValueError('Recovery did not satisfy triangle budget')
    save(result,out/'optimized.glb')
    atomic_json(out/'optimization.json',{'input_faces':before,'output_faces':len(result.faces),'target':target,
                'method':'PyMeshLab bounded quadric recovery after strict topology decimation failed',
                'passes':passes,'source':file_info(source),'implementation':file_info(__file__),
                'uv_policy':'before PBR; no textured UVs are discarded','requires_final_visual_review':True})


if __name__=='__main__':optimize(sys.argv[1],sys.argv[2],int(sys.argv[3]))
