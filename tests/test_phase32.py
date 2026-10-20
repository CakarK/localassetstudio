"""Fake VLM contracts + real CPU fixtures. No Shape/PBR inference in these tests."""
import copy
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from tests.runtime import TEST_ROOT
from unittest.mock import patch
import numpy as np
import trimesh
from PIL import Image,ImageDraw
from ai3d_manager.common import AI,ProcessLock,ProductionError,atomic_json,file_info,intact,canonical
from ai3d_manager.catalog import load_catalog
from ai3d_manager.database import Database
from ai3d_manager.worker import Worker,settings_for
from ai3d_manager.quality_store import QualityStore
from ai3d_manager.gate_analysis import profile_for,reference_analysis,geometry_analysis
from ai3d_manager.quality_gates import visual_decision,merge_decisions,apply_gate_policy,gate_summary,QualityGateService,gate_schema,visual_cache_current,VIEWS
from ai3d_manager.quality_vlm import OllamaVLM,GPUQualityLease
from ai3d_manager.quality_config import load_quality
from ai3d_manager.quality_planner import compare_candidates
from ai3d_manager.quality_schema import parse_response
from ai3d_manager.cli import main
from tests.test_manager import FakeWorker
from tests.test_quality import fixture_payload
from ai3d_manager.quality_requirements import requirements
from ai3d_manager.quality_scoring import score

class Phase32Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.asset=next(a for a in load_catalog() if a['asset_id']=='DemoCabin')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name)
        self.db=Database(self.root/'jobs.sqlite',self.root/'outputs');self.db.sync_assets([self.asset]);self.store=QualityStore(self.root/'quality.sqlite',self.root/'quality')
        self.profile=profile_for(self.asset)
    def tearDown(self):
        self.db.close();self.store.close();assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for n in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if n==7:raise
                time.sleep(.05*(n+1))
    def images(self,bbox=(45,40,210,215)):
        rgb=Image.new('RGB',(256,256),(80,80,80));rgba=rgb.convert('RGBA');mask=Image.new('L',rgb.size);ImageDraw.Draw(mask).rectangle(bbox,fill=255);rgba.putalpha(mask)
        a=self.root/'reference.png';b=self.root/'processed.png';rgb.save(a);rgba.save(b);return a,b
    def analyze_mesh(self,extents=(4,3,5),kind='shelter',mesh=None):
        source=self.root/'fixture.glb';(mesh if mesh is not None else trimesh.creation.box(extents=extents)).export(source)
        return geometry_analysis(source,profile_for(self.asset,kind),self.root)
    @unittest.skipUnless((AI/'Tests/phase32-model-preflight.json').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_qwen_config_and_installed_digest(self):
        c=load_quality();self.assertEqual(c['model'],'qwen3.6:27b');self.assertEqual(c['minimum_free_mib'],19000)
        p=json.loads((AI/'Tests/phase32-model-preflight.json').read_text('utf-8'));self.assertEqual(p['qwen']['digest'],c['model_digests'][c['model']]);self.assertIn('vision',p['show']['capabilities'])
        explicit=load_quality({'model':'gemma3:4b'});self.assertEqual(explicit['protocol'],'grounded-compact-3.1');self.assertEqual(explicit['num_ctx'],8192)
        self.assertEqual(load_quality()['model'],'qwen3.6:27b')
    def test_empty_mask_is_fail(self):
        a,b=self.images();Image.new('RGBA',(256,256),(0,0,0,0)).save(b);self.assertEqual(reference_analysis(a,b,self.profile)['status'],'FAIL')
    def test_frame_clipping_is_review(self):
        a,b=self.images((0,0,255,230));r=reference_analysis(a,b,self.profile)
        self.assertTrue(any(c['check']=='frame_edge_contact' and c['status']=='NeedsReview' for c in r['checks']))
    def test_low_foreground_is_category_specific_review(self):
        a,b=self.images((20,120,235,123));r=reference_analysis(a,b,self.profile);self.assertEqual(r['status'],'NeedsReview')
        f=reference_analysis(a,b,profile_for(self.asset,'fence'));self.assertEqual(f['status'],'PASS')
    def test_wrong_foreground_color_requires_review(self):
        a,b=self.images();im=Image.open(b);alpha=im.getchannel('A');im=Image.new('RGBA',im.size,'red');im.putalpha(alpha);im.save(b)
        self.assertTrue(any(c['check']=='foreground_rgb_preservation' and c['status']=='NeedsReview' for c in reference_analysis(a,b,self.profile)['checks']))
    def test_fragmented_mask_components(self):
        a,b=self.images();im=Image.open(b);alpha=Image.new('L',im.size);draw=ImageDraw.Draw(alpha)
        draw.rectangle((25,50,85,200),fill=255);draw.rectangle((170,50,230,200),fill=255);im.putalpha(alpha);im.save(b)
        r=reference_analysis(a,b,self.profile);self.assertEqual(r['metrics']['significant_components'],2);self.assertEqual(r['status'],'NeedsReview')
    def test_off_center_reference(self):
        a,b=self.images((1,60,55,200));r=reference_analysis(a,b,self.profile)
        self.assertTrue(any(c['check']=='object_center' and c['status']=='NeedsReview' for c in r['checks']))
    def test_unknown_class_is_not_silent_pass(self):
        a,b=self.images();p=profile_for({'asset_id':'new_unknown','category':'Unknown'});self.assertEqual(p['name'],'unknown')
        self.assertEqual(reference_analysis(a,b,p)['status'],'NeedsReview')
    def test_visual_wrong_identity_fails(self):
        v={'criteria':{'expected_identity':{'result':'fail','confidence':.9,'views':['reference'],'evidence':'FAKE roof fragment','uncertainty_reason':''}}}
        self.assertEqual(visual_decision(v),'FAIL')
    def test_visual_uncertainty_needs_reason(self):
        v={'criteria':{'expected_identity':{'result':'uncertain','confidence':.8,'views':['reference'],'evidence':'FAKE','uncertainty_reason':''}}}
        with self.assertRaises(ValueError):visual_decision(v)
    def test_high_visual_score_cannot_override_identity_fail(self):
        report={'overall_score':100,'decision':'VisuallyApprovedCandidate','review_reasons':[]}
        r=apply_gate_policy(report,{'status':'FAIL','geometry':{'status':'FAIL'},'reference':{'status':'PASS'}})
        self.assertEqual(r['overall_score'],100);self.assertEqual(r['decision'],'NeedsReview');self.assertFalse(r['automatic_acceptance_allowed'])
        self.assertEqual(merge_decisions('FAIL','PASS'),'FAIL')
    def test_narrow_building_rejected_but_fence_allowed(self):
        self.assertEqual(self.analyze_mesh((.08,2,8))['status'],'FAIL');self.assertEqual(self.analyze_mesh((.08,2,8),'fence')['status'],'PASS')
    def test_flat_building_rejected_but_debris_allowed(self):
        self.assertEqual(self.analyze_mesh((5,.1,7))['status'],'FAIL');self.assertEqual(self.analyze_mesh((5,.15,7),'debris')['status'],'PASS')
    def test_open_fragment_has_no_trusted_volume(self):
        m=trimesh.creation.box(extents=(4,3,5));m.update_faces(np.arange(len(m.faces))!=0)
        r=self.analyze_mesh(mesh=m);self.assertFalse(r['metrics']['volume_reliable']);self.assertIsNone(r['metrics']['enclosed_volume_native_units_cubed'])
    def test_components_are_class_aware(self):
        meshes=[]
        for n in range(6):
            m=trimesh.creation.box();m.apply_translation(((n%3)*1.5,0,(n//3)*1.5));meshes.append(m)
        scene=trimesh.util.concatenate(meshes);r=self.analyze_mesh(mesh=scene);d=self.analyze_mesh(mesh=scene,kind='debris')
        self.assertEqual(r['metrics']['component_count'],6);self.assertEqual(r['status'],'NeedsReview');self.assertEqual(d['status'],'PASS')
    def test_watertight_native_volume_and_surface_occupancy(self):
        r=self.analyze_mesh();m=r['metrics'];self.assertTrue(m['volume_reliable']);self.assertAlmostEqual(m['enclosed_volume_native_units_cubed'],60)
        self.assertIn('NOT filled/physical volume',m['surface_shell_occupancy']['method']);self.assertIn('top',m['projected_silhouettes'])
    def test_tree_and_tower_are_not_shelter(self):
        self.assertEqual(self.analyze_mesh((.8,14,.7),'tower')['status'],'PASS');self.assertEqual(self.analyze_mesh((.4,12,.3),'tree')['status'],'PASS')
        self.assertEqual(self.analyze_mesh((.8,14,.7),'shelter')['status'],'FAIL')
    def orchestrator(self,reference='PASS',geometry='PASS'):
        statuses={'reference':reference,'geometry':geometry};calls=[]
        class Gates:
            def __init__(self,*args):pass
            def run(self,kind,*args,**kwargs):
                calls.append(kind);return {'report_id':'FAKE-'+kind,'status':statuses[kind],'work_dir':'FAKE-only test report'}
        class FixtureWorker(Worker):
            def __init__(self,db):super().__init__(db,gate_service_factory=Gates);self.calls=[];self.fail=None
            execute_stage=FakeWorker.execute_stage
        return FixtureWorker,statuses,calls
    def test_reference_gate_stops_before_shape(self):
        cls,statuses,calls=self.orchestrator(reference='FAIL');job=self.db.enqueue(self.asset,'ref_fail',settings_for(self.asset))[0];w=cls(self.db);r=w.run(job['id'])
        self.assertEqual(r['status'],'NeedsReview');self.assertNotIn('GeometryGenerated',w.calls);self.assertNotIn('PBRGenerated',w.calls)
        self.assertEqual(json.loads(r['error_json'])['category'],'ReferenceGateFailed')
    def test_geometry_gate_stops_before_pbr(self):
        cls,statuses,calls=self.orchestrator(geometry='FAIL');job=self.db.enqueue(self.asset,'geo_fail',settings_for(self.asset))[0];w=cls(self.db);r=w.run(job['id'])
        self.assertEqual(w.calls.count('GeometryGenerated'),1);self.assertNotIn('PBRGenerated',w.calls);self.assertEqual(r['status'],'NeedsReview')
    def test_resume_retry_never_regenerates_on_failed_gate(self):
        cls,statuses,calls=self.orchestrator(geometry='FAIL');job=self.db.enqueue(self.asset,'resume',settings_for(self.asset))[0];cls(self.db).run(job['id'])
        prior=len(self.db.attempts(job['id']));self.db.resume(job['id']);w=cls(self.db);w.run(job['id']);self.assertFalse(w.calls);self.assertEqual(len(self.db.attempts(job['id'])),prior)
        statuses['geometry']='PASS';self.db.resume(job['id']);w=cls(self.db);self.assertEqual(w.run(job['id'])['status'],'Completed');self.assertNotIn('GeometryGenerated',w.calls);self.assertEqual(w.calls.count('PBRGenerated'),1)
    def test_parent_lock_ownership_cannot_be_forged(self):
        with self.assertRaises(ProductionError):
            with GPUQualityLease(OllamaVLM(),self.root,parent_lock=ProcessLock(self.root/'not-production.lock')):pass
    def test_nested_gate_lease_keeps_parent_lock_and_reservation(self):
        from tests.test_quality import Response
        class HTTP:
            def get(self,url,**kwargs):
                if url.endswith('/queue'):return Response({'queue_running':[],'queue_pending':[]})
                if url.endswith('/ps'):return Response({'models':[]})
                return Response({'system':{'argv':[str(AI/'ComfyUI/main.py')]}})
            def post(self,*args,**kwargs):return Response({})
        path=self.root/'nested.lock';vlm=OllamaVLM(session=HTTP())
        with ProcessLock(path) as parent:
            with GPUQualityLease(vlm,self.root,lock_path=path,parent_lock=parent,sampler=lambda:{'free_mib':22000,'used_mib':0}) as lease:
                lease.mark_owned();self.assertTrue(lease.marker.exists())
            self.assertFalse(lease.marker.exists())
            with self.assertRaises(ProductionError):
                with ProcessLock(path):pass
        with ProcessLock(path):pass
    @unittest.skipUnless((AI/'Backups/phase_3_2/production.json').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_existing_production_and_quality_rows_unchanged(self):
        old=json.loads((AI/'Backups/phase_3_2/production.json').read_text('utf-8'));db=Database()
        from tests.preservation import historical_rows
        for table,rows in old.items():self.assertEqual(historical_rows(db,table,rows),rows)
        db.close()
    @unittest.skipUnless((AI/'Backups/phase_3_2/preserved-files.json').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_old_artifact_hash_preservation(self):
        old=json.loads((AI/'Backups/phase_3_2/preserved-files.json').read_text('utf-8'))
        for p,info in old.items():self.assertTrue(intact({'path':p,**info}),p)
    def test_gate_report_cli_is_production_read_only(self):
        j=self.db.enqueue(self.asset,'read_only',settings_for(self.asset))[0];before=self.db.job(j['id'])
        args=['--db',str(self.db.path),'--quality-db',str(self.store.path),'--quality-root',str(self.root/'quality'),'quality','gates-report',j['id']]
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(args),0)
        self.assertEqual(self.db.job(j['id']),before);self.assertFalse(self.db.attempts(j['id']))
    def test_missing_gate_blocks_manual_approval_and_selection(self):
        j=self.db.enqueue(self.asset,'high_score',settings_for(self.asset))[0];e=self.store.begin(j,{})
        interpretation=requirements(self.asset);r=score(fixture_payload(interpretation,100),interpretation,{'status':'PASS','checks':[]},[{'view':v,'width':768} for v in ['front','back','left','right','isometric_45','top']])
        r.update(job_id=j['id'],asset_id=self.asset['asset_id'],evaluation_id=e['id']);self.store.finish(e['id'],r)
        self.assertIsNone(compare_candidates(self.db,self.store,self.asset['asset_id'])['recommended_job_id'])
        with patch('ai3d_manager.quality_gates.gate_summary',return_value={'status':'FAIL','reference':{'status':'PASS'},'geometry':{'status':'FAIL'}}):
            comparison=compare_candidates(self.db,self.store,self.asset['asset_id'])
            self.assertIsNone(comparison['recommended_job_id']);self.assertIsNone(comparison['best_review_candidate_job_id'])
        args=['--db',str(self.db.path),'--quality-db',str(self.store.path),'--quality-root',str(self.root/'quality'),'quality','approve',j['id'],'--reason','FAKE explicit human request']
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):self.assertEqual(main(args),2)
        self.assertFalse(self.store.actions(j['id']))
    def test_gate_schema_rejects_invented_evidence_view(self):
        data={'criteria':{r:{'result':'pass','confidence':.9,'views':['invented'],'evidence':'FAKE','uncertainty_reason':''} for r in ['expected_identity','whole_object','framing','mask_preserves_object','category_match','shape_suitability']},'summary':'FAKE'}
        with self.assertRaises(Exception):parse_response({'done':True,'message':{'content':json.dumps(data)}},gate_schema('reference',['reference','foreground','mask']))

    def test_full_visual_cache_verifies_all_render_and_input_hashes(self):
        source=self.root/'fake-source.glb';source.write_text('FAKE cache source only');model=file_info(source);artifacts=[]
        for view in VIEWS:
            p=self.root/(view+'.png');Image.new('RGB',(32,32),'gray').save(p)
            artifacts.append({'mode':'material','view':view,**file_info(p),'vlm_input':file_info(p)})
        report={'scores':{'fake':100},'source_model':model,'evaluation_protocol':{'profile':load_quality()},'render_artifacts':artifacts}
        self.assertTrue(visual_cache_current(report,model,load_quality()))
        (self.root/'top.png').write_text('tampered')
        self.assertFalse(visual_cache_current(report,model,load_quality()))

if __name__=='__main__':unittest.main(verbosity=2)
