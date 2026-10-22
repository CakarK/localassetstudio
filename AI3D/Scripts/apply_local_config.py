"""Configure only the image ComfyUI; 3D runs in isolated TRELLIS."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
(root/'ComfyUI/extra_model_paths.yaml').write_text('local_asset_studio:\n  base_path: '+(root/'Models').as_posix()+'\n  checkpoints: checkpoints\n',encoding='utf-8')
print('Image model search path configured; no 3D custom nodes installed')
