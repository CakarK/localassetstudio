"""TRELLIS native Z-up <-> the existing GLB Y-up production convention."""
import numpy as np


def native_to_gltf(vertices):
    result=np.asarray(vertices)[:,[0,2,1]].copy()
    result[:,2]*=-1
    return result


def gltf_to_native(vertices):
    result=np.asarray(vertices)[:,[0,2,1]].copy()
    result[:,1]*=-1
    return result
