"""Tests use synthetic fixtures; no private game prompts are distributed."""
import os
from pathlib import Path
os.environ['LAS_CATALOG_PATH'] = str(Path(__file__).parent / 'fixtures/catalog.json')
