"""Provider selection and inference options live in a versioned JSON profile."""
import json
import math
from .common import AI

CONFIG=AI/'Config/quality.json'

def load_quality(overrides=None,path=None):
    root=json.loads((path or CONFIG).read_text('utf-8'));data=dict(root['quality']);overrides=overrides or {}
    selected=overrides.get('model',data['model'])
    if selected!=data['model']:data.update(root.get('model_profiles',{}).get(selected,{}))
    data.update(overrides)
    if data['provider']!='ollama':raise ValueError('Only local Ollama is supported')
    if not isinstance(data['model'],str) or not data['model'] or 'cloud' in data['model']:raise ValueError('Explicit local vision model required')
    for key in ('num_ctx','max_output_tokens','minimum_free_mib'):
        if type(data[key]) is not int or data[key]<=0:raise ValueError('Invalid '+key)
    if not isinstance(data['temperature'],(int,float)) or not math.isfinite(data['temperature']) or not 0<=data['temperature']<=2:raise ValueError('Invalid temperature')
    if data['image_strategy'] not in ('six','split3'):raise ValueError('Every supported strategy must cover all six views')
    if data['allow_automatic_visual_approval'] is not False:raise ValueError('Human visual approval cannot be enabled automatically')
    return data
