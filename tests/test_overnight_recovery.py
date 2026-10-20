"""Production regressions found in the October 20 overnight batch. No real VLM."""
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock,patch
from PIL import Image
from ai3d_manager.catalog import load_catalog
from ai3d_manager.common import ProductionError
from ai3d_manager.quality_config import load_quality
from ai3d_manager.quality_requirements import requirements
from ai3d_manager.quality_service import QualityService,VIEWS
from ai3d_manager.web_control import web_gate_profile
from tests.test_quality import fixture_payload
from tests.runtime import TEST_ROOT


@contextmanager
def isolated_directory():
    temp=tempfile.TemporaryDirectory(dir=TEST_ROOT)
    try:yield temp.name
    finally:
        assert Path(temp.name).resolve().is_relative_to(TEST_ROOT.resolve())
        for retry in range(6):
            try:temp.cleanup();break
            except OSError as exc:
                if retry==5 or getattr(exc,'winerror',None) not in (5,32,145):raise
                time.sleep(.1*(retry+1))


class OvernightRecoveryTests(unittest.TestCase):
    def test_known_catalog_objects_use_general_geometry_rules_only_on_web(self):
        from ai3d_manager.gate_analysis import profile_for
        assets={a['asset_id']:a for a in load_catalog()}
        for ident in ('DemoFigure',):
            with self.subTest(asset=ident):
                self.assertEqual(profile_for(assets[ident])['name'],'prop')
                self.assertEqual(web_gate_profile(assets[ident])['name'],'prop')
                self.assertEqual(web_gate_profile({**assets[ident],'phase35_gate_profile':'fence'})['name'],'fence')
        self.assertEqual(web_gate_profile({'asset_id':'Unmapped','category':'Other'})['name'],'unknown')

    def test_invalid_optional_extraction_continues_real_visual_scoring_without_caching_bad_quotes(self):
        for category,fallback in (('VLMInvalidResponse',True),('VLMInvalidResponse',False),('CUDA_OOM',True),('VLMHTTP',True)):
            with self.subTest(category=category,fallback=fallback), isolated_directory() as directory:
                work=Path(directory);asset=load_catalog()[0];base=requirements(asset)
                job={'id':'test-job','asset_id':asset['asset_id'],'status':'Completed',
                     'request_json':json.dumps(asset),'settings_json':json.dumps({'comfy_url':'http://127.0.0.1:8190','blender':'unused'})}
                model={'path':str(work/'model.glb'),'sha256':'fixture-only','size':1}
                outputs={'Scaled':{'model':model},'Validated':{'validation':model}}
                artifacts=[]
                for view in VIEWS:
                    p=work/(view+'.png');Image.new('RGB',(512,512),'gray').save(p)
                    artifacts.append({'view':view,'mode':'material','path':str(p)})
                metrics={'elapsed_seconds':1,'load_seconds':0,'prompt_eval_count':1,'eval_count':1}
                vlm=Mock();vlm.profile=load_quality();vlm.profile.update(protocol='qwen-evidence-3.2',image_strategy='six')
                vlm.model='FAKE';vlm.metadata.return_value={'installed':{'digest':'FAKE'}}
                def structured(prompt,schema,directory,name,**kw):
                    if name=='requirements':raise ProductionError(category,'FAKE invalid exact quote or runtime failure')
                    return fixture_payload(base),metrics
                vlm.structured.side_effect=structured
                store=Mock();store.begin.return_value={'id':'evaluation','job_id':job['id'],'status':'Running','work_dir':str(work/'eval')}
                (work/'eval').mkdir()
                lease=Mock();lease.__enter__=Mock(return_value=lease);lease.__exit__=Mock(return_value=False)
                lease.placement.return_value={'verified':True,'cpu_offload_detected':False}
                with patch('ai3d_manager.quality_service.production_artifacts',return_value=(job,outputs,{'status':'PASS','checks':[]})), \
                     patch('ai3d_manager.quality_service.reusable_render',return_value=None), \
                     patch('ai3d_manager.quality_service.run_process'), \
                     patch('ai3d_manager.quality_service.map_renders',return_value=({},artifacts)), \
                     patch('ai3d_manager.quality_service.CriteriaCache') as cache, \
                     patch('ai3d_manager.quality_gates.gate_summary',return_value={'status':'PASS'}):
                    cache.return_value.find_existing.return_value=(None,'miss')
                    report=QualityService(Mock(),store,vlm=vlm,lease_factory=Mock(return_value=lease),
                                          fallback_invalid_extraction=fallback).evaluate(job['id'])
                    cache.return_value.put.assert_not_called()
                if category=='VLMInvalidResponse' and fallback:
                    self.assertTrue(report['scores']);store.finish.assert_called_once()
                    self.assertEqual(report['performance']['criteria_cache'],'template_after_invalid_ai_extraction')
                    self.assertTrue((work/'eval/requirements-fallback.json').is_file())
                    self.assertTrue(any(c.kwargs.get('images') for c in vlm.structured.call_args_list))
                else:
                    self.assertIsNone(report['scores']);store.finish.assert_not_called()
                    self.assertEqual(report['error']['category'],category)


if __name__=='__main__':unittest.main()
