"""Download exactly the supported full-pipeline weights; never submit inference."""
import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from install_support import confined, download, verify, write_json, installed_ollama_models, ollama_manifest_matches

ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / 'AI3D'


def json_api(path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request('http://127.0.0.1:11434' + path, data=body,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def install_ollama(lock):
    missing = installed_ollama_models(json_api('/api/tags'), lock['ollama_models'])
    # Never download into a foreign server's model store. A server we started
    # must be the live process in this installation's PID receipt.
    if missing:
        import psutil
        receipt = AI / 'State/ollama.pid'
        runtime = json.loads((AI / 'State/install-runtime.json').read_text('utf-8-sig'))
        try:
            process = psutil.Process(int(receipt.read_text().strip()))
            expected = Path(runtime['ollama']).resolve()
            owned = Path(process.exe()).resolve() == expected and 'serve' in process.cmdline()
            listeners = {c.pid for c in psutil.net_connections(kind='tcp') if c.laddr and c.laddr.port == 11434 and c.status == 'LISTEN'}
            owned = owned and process.pid in listeners
        except (OSError, ValueError, psutil.Error):
            owned = False
        if not owned:
            raise RuntimeError('Port 11434 belongs to an existing Ollama installation with missing models. Quit that service yourself, then rerun setup. No process/model was changed.')
    for spec in missing:
        model, tag = spec['model'].split(':', 1)
        url = f'https://registry.ollama.ai/v2/library/{model}/manifests/{tag}'
        request = urllib.request.Request(url, headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            ollama_manifest_matches(response.read(), spec['sha256'])
        print('OLLAMA PULL', spec['model'], flush=True)
        request = urllib.request.Request('http://127.0.0.1:11434/api/pull', data=json.dumps({'model': spec['model'], 'stream': True}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=120) as response:
            import time
            last = 0
            for line in response:
                progress = json.loads(line)
                if progress.get('error'):
                    raise RuntimeError(progress['error'])
                if time.monotonic() - last > 15 or progress.get('status') == 'success':
                    print(spec['model'], progress.get('status'), progress.get('completed', 0), '/', progress.get('total', 0), flush=True)
                    last = time.monotonic()
        # This also catches a tag changing between registry check and pull.
        installed_ollama_models(json_api('/api/tags'), [spec])
    if installed_ollama_models(json_api('/api/tags'), lock['ollama_models']):
        raise RuntimeError('Ollama pull did not install all required models')
    metadata = json_api('/api/show', {'model': 'qwen3.6:27b'})
    if 'vision' not in metadata.get('capabilities', []):
        raise RuntimeError('Installed Qwen does not advertise vision support')
    write_json(AI / 'State/install-ollama-verification.json', {'status': 'PASS', 'models': lock['ollama_models'], 'vision': True, 'inference_run': False})


def install_files(lock):
    os.environ['HF_HOME'] = str(AI / 'Models/huggingface')
    from huggingface_hub import hf_hub_download
    report = []
    for spec in lock['files']:
        path = confined(ROOT, spec['path'])
        url = spec.get('url') or f'https://huggingface.co/{spec["repository"]}/resolve/{spec["revision"]}/{spec["remote_file"]}'
        print('MODEL', path.name, flush=True)
        download(url, path, spec)
        report.append({**spec, 'status': 'PASS'})
    write_json(AI / 'State/install-model-verification.json', {'status': 'PASS', 'files': report})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ollama-only', action='store_true')
    args = parser.parse_args()
    import truststore
    truststore.inject_into_ssl()
    lock = json.loads((AI / 'install.lock.json').read_text('utf-8'))
    install_ollama(lock) if args.ollama_only else install_files(lock)


if __name__ == '__main__':
    main()
