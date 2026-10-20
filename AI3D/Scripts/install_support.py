"""Verified, resumable setup I/O. Imports do not install or start anything."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def confined(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Installation path escapes its root: ' + str(relative))
    return path


def verify(path, spec):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != spec['size'] or sha256(path) != spec['sha256']:
        raise ValueError('Size/SHA256 mismatch; existing file preserved: ' + str(path))
    return path


def download(url, destination, spec, opener=None):
    """Resume a partial GET, handle ignored Range, never overwrite an old target."""
    target = Path(destination)
    if target.exists():
        return verify(target, spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + '.partial')
    opener = opener or urllib.request.urlopen
    for attempt in range(3):
        offset = part.stat().st_size if part.exists() else 0
        if offset == spec['size']:
            break
        if offset > spec['size']:
            raise ValueError('Oversized partial file preserved: ' + str(part))
        headers = {'User-Agent': 'LocalAssetStudio-setup'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=60) as response:
                resumed = response.status == 206
                if resumed and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('Server returned an incorrect resume range')
                # A server may ignore Range and return a full 200. Restart this
                # exact owned partial, rather than appending duplicated bytes.
                count = offset if resumed else 0
                reported = time.monotonic()
                with part.open('ab' if resumed else 'wb') as stream:
                    while block := response.read(8 * 1024 * 1024):
                        stream.write(block)
                        count += len(block)
                        if count > spec['size']:
                            raise ValueError('Download exceeds pinned size')
                        if time.monotonic() - reported > 15:
                            print(f'{target.name}: {count / 1e9:.2f}/{spec["size"] / 1e9:.2f} GB', flush=True)
                            reported = time.monotonic()
                if count < spec['size']:
                    raise OSError('Truncated download; partial file kept for resume: ' + str(part))
            break
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(1)
    try:
        verify(part, spec)
    except ValueError:
        if part.exists():
            part.rename(part.with_name(part.name + f'.bad-{time.time_ns()}'))
        raise
    # Only a completely verified file becomes visible at the final name.
    if target.exists():
        return verify(target, spec)
    part.rename(target)
    return target


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', 'utf-8')
    os.replace(temporary, path)


def ollama_manifest_matches(body, expected):
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('Public Ollama tag changed since install.lock.json; no unpinned pull was performed.')


def installed_ollama_models(tags, specs):
    installed = {m['name']: m['digest'].removeprefix('sha256:') for m in tags.get('models', [])}
    missing = []
    for spec in specs:
        found = installed.get(spec['model'])
        if found is None:
            missing.append(spec)
        elif found != spec['sha256']:
            raise ValueError('Existing Ollama model has a different digest; preserved: ' + spec['model'])
    return missing
