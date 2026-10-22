"""MIT licensed UV rasterizer for local texture baking; no NVIDIA research SDK.

Pixel row 0 corresponds to UV v=0, matching the original O-Voxel exporter.
The upstream exporter subsequently flips its glTF UV coordinates.
"""
import numpy as np


def rasterize_uv_positions(vertices, faces, uvs, texture_size):
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int64)
    uvs = np.asarray(uvs, dtype=np.float64)
    size = int(texture_size)
    if size < 1 or size > 8192:
        raise ValueError("Texture size must be between 1 and 8192")
    if vertices.ndim != 2 or vertices.shape[1] != 3 or uvs.shape != (len(vertices), 2):
        raise ValueError("Vertex/UV dimensions disagree")
    if faces.ndim != 2 or faces.shape[1] != 3 or not len(faces):
        raise ValueError("Expected nonempty triangle faces")
    if faces.min() < 0 or faces.max() >= len(vertices):
        raise ValueError("Face index outside vertex array")
    if not np.isfinite(vertices).all() or not np.isfinite(uvs).all():
        raise ValueError("Non-finite geometry/UV")
    mask = np.zeros((size, size), dtype=bool)
    positions = np.zeros((size, size, 3), dtype=np.float32)
    for face in faces:
        tri = uvs[face] * size - 0.5
        x0, y0 = np.maximum(np.ceil(tri.min(axis=0)).astype(np.int64), 0)
        x1, y1 = np.minimum(np.floor(tri.max(axis=0)).astype(np.int64), size - 1)
        if x0 > x1 or y0 > y1:
            continue
        a, b, c = tri
        den = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1])
        if abs(den) < 1e-12:
            continue
        xx, yy = np.meshgrid(np.arange(x0, x1+1), np.arange(y0, y1+1))
        w0 = ((b[1]-c[1])*(xx-c[0]) + (c[0]-b[0])*(yy-c[1])) / den
        w1 = ((c[1]-a[1])*(xx-c[0]) + (a[0]-c[0])*(yy-c[1])) / den
        w2 = 1-w0-w1
        inside = (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7)
        yy, xx = yy[inside], xx[inside]
        weights = np.stack((w0[inside], w1[inside], w2[inside]), axis=-1)
        positions[yy, xx] = weights @ vertices[face]
        mask[yy, xx] = True
    if not mask.any():
        raise ValueError("UV atlas covers no texture pixels")
    return positions, mask
