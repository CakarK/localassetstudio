"""Existing colored references and geometry can continue to PBR without new Shape."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import trimesh
from ai3d_manager.common import ProductionError,file_info,intact
from ai3d_manager.web_assets import asset_from_request
from ai3d_manager.web_control import InteractiveWorker,web_gate_profile
from tests.runtime import TEST_ROOT


class MaterialContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name)
        self.asset=asset_from_request({'source_mode':'custom','name':'Rock cluster',
            'prompt':'A complete cluster of grey granite boulders.','prompt_language':'en',
            'gate_profile':'prop','polygon_target':2500,'footprint_m':[3,3]})
        geometry=self.root/'geometry.glb';trimesh.creation.icosphere(subdivisions=1).export(geometry)
        image=self.root/'reference.png';Image.new('RGB',(256,256),(112,91,60)).save(image)
        mask=self.root/'preprocessed.png';Image.new('RGBA',(256,256),(112,91,60,255)).save(mask)
        self.sources={'GeometryGenerated':{'geometry':file_info(geometry)},
            'ReferenceGenerated':{'reference':file_info(image)},'ReferenceProcessed':{'preprocessed':file_info(mask)}}
    def tearDown(self):
        assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for attempt in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if attempt==7:raise
                time.sleep(.05*(attempt+1))

    def test_rock_prop_has_no_building_identity_requirement(self):
        self.assertEqual(self.asset['category'],'Environment')
        profile=web_gate_profile(self.asset)
        self.assertEqual(profile['name'],'prop')
        self.assertNotIn('building',profile['object_class'])
        building=web_gate_profile({**self.asset,'category':'Buildings','phase35_gate_profile':'building'})
        self.assertIn('building',building['object_class'])

    def test_replayed_reference_mask_and_shape_preserve_bytes_and_do_not_infer(self):
        for stage,source in self.sources.items():
            worker=InteractiveWorker(SimpleNamespace(),stage)
            work=self.root/stage;work.mkdir()
            with patch.object(worker,'gpu_stage',side_effect=AssertionError('No new inference permitted')):
                result=worker.execute_stage(stage,{'job_id':'fixture','work_dir':str(work)},self.asset,{'reuse_files':self.sources})
            for name,info in source.items():
                self.assertEqual(file_info(result[name])['sha256'],info['sha256'])
                self.assertTrue(intact(info))
                self.assertNotEqual(str(result[name]),info['path'])
            import json
            origin=json.loads(result['origin'].read_text('utf-8'))
            self.assertTrue(origin['not_new_inference'])
            self.assertEqual(origin['sources'],source)
        with Image.open(self.root/'ReferenceGenerated/reference.png') as image:
            self.assertEqual(image.getpixel((128,128)),(112,91,60))

    def test_changed_geometry_blocks_replay_before_any_gpu_call(self):
        info=self.sources['GeometryGenerated']['geometry'];Path(info['path']).write_bytes(b'tampered')
        worker=InteractiveWorker(SimpleNamespace(),'GeometryGenerated');work=self.root/'attempt';work.mkdir()
        with patch.object(worker,'gpu_stage',side_effect=AssertionError('No GPU call')):
            with self.assertRaises(ProductionError):worker.execute_stage('GeometryGenerated',{'job_id':'fixture','work_dir':str(work)},self.asset,{'reuse_files':self.sources})


if __name__=='__main__':unittest.main()
