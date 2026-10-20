"""Installation smoke checks; no image, VLM, shape or PBR inference."""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from install_support import write_json, installed_ollama_models

ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / 'AI3D'
sys.path.insert(0, str(ROOT))


def cuda_probe(python, expected, extensions=False):
    code = '''import torch,json
assert torch.__version__ == EXPECTED, torch.__version__
assert torch.cuda.is_available(), 'CUDA unavailable; check NVIDIA driver'
x=torch.ones(4,device='cuda');assert x.sum().item()==4
EXTENSIONS
print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'device':torch.cuda.get_device_name(0),'status':'PASS'}))
'''.replace('EXPECTED', repr(expected)).replace('EXTENSIONS', 'import custom_rasterizer,mesh_inpaint_processor' if extensions else '')
    process = subprocess.run([str(python), '-c', code], capture_output=True, text=True, timeout=120,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if process.returncode:
        raise RuntimeError(process.stderr[-4000:] or process.stdout[-4000:])
    return json.loads(process.stdout.splitlines()[-1])


def api(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def services():
    from ai3d_manager.image_providers import validate_graph, ImageGenerationProvider
    checks = {}
    for port, expected in [(8190, AI / 'ComfyUI/main.py'), (8191, AI / 'ImageComfyUI/main.py')]:
        stats = api(f'http://127.0.0.1:{port}/system_stats')
        if not any(Path(a).resolve() == expected.resolve() for a in stats['system']['argv'] if isinstance(a, str)):
            raise RuntimeError(f'Port {port} belongs to a different installation')
        nodes = api(f'http://127.0.0.1:{port}/object_info')
        if port == 8190:
            required = ['Hy3D21MeshGenerator', 'Hy3D21VAELoader', 'Hy3D21VAEDecode', 'G3DCManagedSaveMesh', 'G3DCManagedLoadMesh', 'LocalAssetStudioPBR', 'LocalAssetStudioImageCheckpoint']
            # Node names are checked directly before any graph is submitted.
            absent = [name for name in required if name not in nodes]
            if absent:
                raise RuntimeError('Missing core nodes: ' + ', '.join(absent))
        for ident in (['dreamshaper8'] if port == 8190 else ['qwen-image-2512', 'z-image-turbo']):
            provider = ImageGenerationProvider(ident)
            validate_graph(provider.graph('install_check', {'optimized': 'installation check', 'negative': ''}, 1), nodes)
        checks[str(port)] = {'status': 'PASS', 'node_count': len(nodes), 'inference_submitted': False}
    lock = json.loads((AI / 'install.lock.json').read_text('utf-8'))
    if installed_ollama_models(api('http://127.0.0.1:11434/api/tags'), lock['ollama_models']):
        raise RuntimeError('Missing Ollama models')
    with urllib.request.urlopen('http://127.0.0.1:8088/', timeout=10) as response:
        if response.headers.get('X-Asset-Studio') != 'game3dcreator':
            raise RuntimeError('Port 8088 is not the studio')
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image-python')
    parser.add_argument('--blender')
    parser.add_argument('--services-only', action='store_true')
    args = parser.parse_args()
    if args.services_only:
        result = {'status': 'PASS', 'services': services(), 'inference_run': False}
        write_json(AI / 'State/install-service-verification.json', result)
    else:
        if not args.image_python or not args.blender:
            parser.error('--image-python and --blender are required')
        from ai3d_manager.quality_config import load_quality
        lock = json.loads((AI / 'install.lock.json').read_text('utf-8'))
        expected = next(m['sha256'] for m in lock['ollama_models'] if m['model'] == 'qwen3.6:27b')
        quality = load_quality()
        if quality['model_digests'].get('qwen3.6:27b') != expected:
            raise RuntimeError('Quality profile differs from the installer model lock; no automatic rewrite')
        models = json.loads((AI / 'State/install-model-verification.json').read_text('utf-8'))
        if models['status'] != 'PASS':
            raise RuntimeError('Model verification incomplete')
        core = cuda_probe(sys.executable, '2.6.0+cu126', extensions=True)
        image = cuda_probe(args.image_python, '2.10.0+cu130')
        blender = subprocess.run([args.blender, '--background', '--factory-startup', '--python-expr',
                                  'import bpy; assert bpy.app.version[:3] == (5,0,1); print("LAS_BLENDER_CHECK_PASS")'],
                                 capture_output=True, text=True, timeout=90,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if blender.returncode or 'LAS_BLENDER_CHECK_PASS' not in blender.stdout:
            raise RuntimeError('Blender smoke check failed: ' + blender.stderr[-2000:])
        result = {'status': 'PASS', 'core': core, 'image': image, 'blender': '5.0.1 PASS', 'inference_run': False}
        write_json(AI / 'State/install-verification.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
