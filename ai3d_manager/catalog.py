"""Load a user-owned JSON catalog; no game-specific prompts or dimensions ship."""
import copy
import json
import math
import os
import re
from pathlib import Path
from .common import AI, file_info

DEFAULT_SOURCE = AI / 'Prompts/catalog.example.json'

def load_catalog(source=None, overrides=None):
    source = Path(source if source is not None else os.environ.get('LAS_CATALOG_PATH', DEFAULT_SOURCE))
    data = json.loads(source.read_text('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('assets'), list):
        raise ValueError('Catalog must be an object with an assets array')
    cell = data.get('grid_cell_m')
    if cell is not None and (type(cell) not in (int, float) or not math.isfinite(cell) or cell <= 0):
        raise ValueError('grid_cell_m must be a positive finite number or null')
    style, negative = data.get('shared_style', ''), data.get('shared_negative', '')
    if not isinstance(style, str) or not isinstance(negative, str):
        raise ValueError('Shared style and negative prompt must be strings')
    assets = []; overrides = overrides or {}; source_info = file_info(source)
    for entry in data['assets']:
        if not isinstance(entry, dict): raise ValueError('Asset must be an object')
        ident = entry.get('asset_id', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', ident):
            raise ValueError('Asset ID must be unique ASCII letters/numbers/underscores')
        if not isinstance(entry.get('prompt'), str) or not entry['prompt'].strip():
            raise ValueError('Each asset requires its own nonempty prompt')
        item = {'name_tr': ident, 'name_en': ident, 'category': 'Environment',
                'period': None, 'period_label': None, 'prompt_language': 'en',
                'shared_style': style, 'shared_negative': negative, 'grid': None,
                'footprint_m': None, 'height_m': None, 'polygon_range': [1000, 20000],
                'polygon_target': 10000, 'texture_size': 1024,
                'variants': {'default': '001', 'source_note': '', 'count_min': None, 'count_max': None},
                'status': 'NotGenerated', 'warnings': [], 'origins': {}}
        item.update(copy.deepcopy(entry))
        item['source'] = source_info
        item['grid_cell_m'] = cell
        item['origins'].update(prompt='source_catalog', style='source_catalog', negative='source_catalog',
            polygon_budget='source_catalog' if 'polygon_target' in entry else 'configured_default')
        for field, value in overrides.get(ident, {}).items():
            if field in ('source', 'asset_id'): raise ValueError('Cannot override immutable catalog identity/source')
            item[field] = value; item['origins'][field] = 'explicit_override'
        if item.get('grid') is not None and (item.get('footprint_m') is None or
                ('grid' in overrides.get(ident, {}) and 'footprint_m' not in overrides[ident])):
            if cell is None: raise ValueError('Grid conversion requires an explicit grid_cell_m')
            item['footprint_m'] = [v * cell for v in item['grid']]
            item['origins']['footprint_m'] = 'explicit_grid_cell_m'
        assets.append(item)
    if len({a['asset_id'] for a in assets}) != len(assets): raise ValueError('Duplicate catalog IDs')
    return assets
