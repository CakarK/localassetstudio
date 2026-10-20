import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AI = ROOT / 'AI3D'

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def file_info(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': str(path), 'size': path.stat().st_size, 'sha256': sha}

def intact(info):
    try:
        return Path(info['path']).is_file() and file_info(info['path']) == info
    except (OSError, KeyError):
        return False

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows indexers/OneDrive can briefly hold the destination without delete sharing.
    # Retry this same rename only; never rerun an inference stage for a sharing violation.
    for number in range(8):
        try:os.replace(temp, path);break
        except PermissionError:
            if number==7:raise
            time.sleep(.025*(number+1))

def safe_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('Identifier must use ASCII letters, digits, _ or - (1-80).')
    return value

class ProductionError(RuntimeError):
    def __init__(self, category, message, transient=False, uncertain=False):
        super().__init__(message)
        self.category, self.transient, self.uncertain = category, transient, uncertain

class Halt(RuntimeError):
    pass

class ProcessLock:
    """OS releases this advisory lock even after a process crash."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            self.stream.seek(0)
            if not self.stream.read(1):
                self.stream.write(b'0'); self.stream.flush()
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ProductionError('QueueBusy', 'Another manager worker owns the production lock.') from exc
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
