import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ai3d_manager import blender_runtime as runtime


class BlenderRuntimeTests(unittest.TestCase):
    def test_unconfigured_machine_keeps_legacy_default(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(runtime,'AI',Path(temp)),patch.dict(os.environ,{},clear=True):
            self.assertIn('Blender 5.0',runtime.default_blender())

    def test_project_relative_runtime(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(runtime,'AI',Path(temp)),patch.dict(os.environ,{},clear=True):
            state=Path(temp)/'State';state.mkdir()
            (state/'blender-runtime.json').write_text(json.dumps({'executable':'AI3D/Tools/blender.exe'}))
            self.assertEqual(runtime.default_blender(),str(runtime.ROOT/'AI3D/Tools/blender.exe'))

    def test_environment_takes_precedence_over_local_selection(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(runtime,'AI',Path(temp)),patch.dict(os.environ,{'LOCAL_ASSET_STUDIO_BLENDER':'C:/Custom/blender.exe'}):
            state=Path(temp)/'State';state.mkdir()
            (state/'blender-runtime.json').write_text(json.dumps({'executable':'other.exe'}))
            self.assertEqual(Path(runtime.default_blender()),Path('C:/Custom/blender.exe'))

    def test_explicit_job_blender_setting_is_not_rewritten(self):
        from ai3d_manager.worker import settings_for
        asset={'polygon_target':4500,'polygon_range':[3000,6000],'footprint_m':[4,4],'height_m':None,'texture_size':1024}
        self.assertEqual(settings_for(asset,{'blender':'C:/Pinned/blender.exe'})['blender'],'C:/Pinned/blender.exe')
