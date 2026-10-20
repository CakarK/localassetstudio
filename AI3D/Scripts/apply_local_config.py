"""Idempotent configuration; changes affect only the isolated checkout."""
import json
import shutil
from pathlib import Path

root=Path(__file__).resolve().parents[1]
wrapper=root/'ComfyUI/custom_nodes/ComfyUI-Hunyuan3d-2-1'
custom=root/'ComfyUI/custom_nodes/LocalAssetStudioAI3D'
custom.mkdir(exist_ok=True)
shutil.copyfile(root/'Scripts/rei_nodes.py',custom/'__init__.py')
# Read-only reuse. No mutation of the shared existing model store.
(root/'ComfyUI/extra_model_paths.yaml').write_text('local_asset_studio:\n  base_path: '+(root/'Models').as_posix()+'\n  checkpoints: checkpoints\n',encoding='utf-8')
p=wrapper/'hy3dpaint/utils/multiview_utils.py'
s=p.read_text('utf-8')
if '# LocalAssetStudio: pinned weights' not in s:
    s=s.replace('repo_id=config.multiview_pretrained_path,','repo_id=config.multiview_pretrained_path,\n            revision="0b94677654c57bb9a6b6845cd7b704ccf551d327", # LocalAssetStudio: pinned weights')
s=s.replace('self.pipeline = pipeline.to(self.device)','self.pipeline = pipeline # LocalAssetStudio: preserve CPU offload hooks, avoid full GPU placement')
p.write_text(s,'utf-8')
p=wrapper/'nodes.py'
s=p.read_text('utf-8')
# Optional MeshLib routes have a noncommercial dependency; the shipped game-asset
# workflows use PyMeshLab instead. Never silently expose a restricted route.
for name in ('Hy3D21MeshlibDecimate','Hy3D21SimpleMeshlibDecimate','Hy3D21MeshGenerationBatch','Hy3DHighPolyToLowPolyBakeMultiViewsWithMetaData'):
    s=s.replace(f'    "{name}": {name},',f'    # LocalAssetStudio: {name} disabled (optional MeshLib license)')
p.write_text(s,'utf-8')
for rel in ('nodes.py','hy3dpaint/utils/multiview_utils.py','hy3dpaint/hunyuanpaintpbr/pipeline.py'):
    p=wrapper/rel;s=p.read_text('utf-8')
    notice='# Modified locally for LocalAssetStudio AI3D: compatibility, offloading and optional dependency policy.\n'
    if not s.startswith(notice):p.write_text(notice+s,'utf-8')
p=wrapper/'hy3dpaint/hunyuanpaintpbr/pipeline.py'
s=p.read_text('utf-8')
s=s.replace('self.vae.encode(images.to(dtype))','self.vae.encode(images.to(device=self._execution_device, dtype=dtype))')
s=s.replace(' # LocalAssetStudio: VAE encode hook does not move method inputs','')
p.write_text(s,'utf-8')
# Old backend compatibility: only the upstream exporter uses this new API.
# Our checkpoint/export nodes work with the stable backend without it.
print('Local nodes and model search path configured')
from managed_compat import install_managed_compat
install_managed_compat(root,wrapper)
