"""Local Blender selection; immutable existing job settings remain untouched."""
import json
import os
from pathlib import Path
from .common import AI,ROOT


def default_blender():
    configured=os.environ.get('LOCAL_ASSET_STUDIO_BLENDER') or os.environ.get('LAS_BLENDER_PATH')
    config=AI/'State/blender-runtime.json'
    if not configured and config.is_file():
        configured=json.loads(config.read_text(encoding='utf-8'))['executable']
    if configured:
        path=Path(configured)
        return str(configured) if path.is_absolute() else str(ROOT/path)
    return 'C:/Program Files/Blender Foundation/Blender 5.0/blender.exe'
