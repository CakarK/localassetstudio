"""Isolated inference instance using installed runtime without changing it."""
import os
import sys
import runpy
from pathlib import Path

AI = Path(__file__).resolve().parents[1]
source = Path(os.environ.get('LAS_IMAGE_COMFY_SOURCE',str(AI/'ImageComfyUI'))).resolve()
runtime = AI/'ImageRuntime'
for name in ['input', 'output', 'temp', 'user', 'models']:
    (runtime/name).mkdir(parents=True, exist_ok=True)
paths=runtime/'extra_model_paths.yaml'
if not paths.exists():
    paths.write_text('local_asset_studio:\n  base_path: '+(runtime/'models').as_posix()+'\n  diffusion_models: diffusion_models\n  text_encoders: text_encoders\n  vae: vae\n',encoding='utf-8')
os.environ.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
sys.dont_write_bytecode = True
sys.path.insert(0, str(source)); os.chdir(source)
sys.argv = [str(source/'main.py'), '--listen', '127.0.0.1', '--port', '8191', '--lowvram', '--reserve-vram', '2',
            '--disable-all-custom-nodes', '--disable-auto-launch', '--preview-method', 'none',
            '--base-directory', str(runtime), '--output-directory', str(runtime/'output'),
            '--input-directory', str(runtime/'input'), '--user-directory', str(runtime/'user'),
            '--database-url', 'sqlite:///'+(runtime/'user/comfyui.db').as_posix(),
            '--temp-directory', str(runtime/'temp'), '--extra-model-paths-config', str(runtime/'extra_model_paths.yaml')]
runpy.run_path(str(source/'main.py'), run_name='__main__')
