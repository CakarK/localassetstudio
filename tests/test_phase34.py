"""Thirty Phase3.4 contracts. Fake models/HTTP/stages are not real AI evidence."""
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
import trimesh
from tests.runtime import TEST_ROOT
from tests import test_phase33 as phase33_fixture
from ai3d_manager.common import ROOT,AI,ProductionError,atomic_json,canonical,digest,file_info,intact
from ai3d_manager.catalog import load_catalog
from ai3d_manager.worker import Worker,STAGES,settings_for
from ai3d_manager.image_providers import load_registry,ImageGenerationProvider,provider_id,validate_graph
from ai3d_manager.image_benchmark import ImageBenchmarkService,adapt_prompt,compare_references,rank_key,ref_rows,budget_boundary,require_fidelity,recommend,category_assets
from ai3d_manager.reference_fidelity import extract,schema_for,validate_evidence,fidelity_decision,summarize,current_fidelity,VERSION
from ai3d_manager.controlled_generation import claim_budget
from ai3d_manager.quality_gates import current_gate,outputs_for
from ai3d_manager.quality_config import load_quality

REAL_VERIFY=ImageGenerationProvider.verify


class Phase34Tests(unittest.TestCase):
    def setUp(self):
        phase33_fixture.Phase33Tests.setUpClass();self.fixture=phase33_fixture.Phase33Tests();self.fixture.setUp()
        self.db=self.fixture.db;self.store=self.fixture.store;self.root=self.fixture.root
        self.asset=self.fixture.asset;self.registry=load_registry();self.fidelity_states={};test=self
        self.db.sync_assets([a for a in load_catalog() if a['asset_id'] in {a['asset_id'] for a in load_catalog() if a.get('category_profile') in ('Buildings','Towers','Organic','Debris')}])
        class Fidelity:
            def __init__(self,db,store):self.db=db;self.store=store
            def evaluate(self,jid):
                old=current_fidelity(self.db,self.store,jid)
                if old:return old
                asset=json.loads(self.db.job(jid)['request_json']);criteria=extract(asset);source=outputs_for(self.db,jid)['ReferenceGenerated']['reference'];ident=uuid.uuid4().hex
                status=test.fidelity_states.get(asset.get('image_provider'),'PASS');rows=[]
                for r in criteria['requirements']:
                    rows.append({**r,'result':'Met' if r['visually_evaluable'] else 'NotObservable','confidence':.9 if r['visually_evaluable'] else None,'evidence':'FAKE source evidence','related_images':['reference'] if r['visually_evaluable'] else [],'uncertainty_reason':''})
                report={'report_id':ident,'job_id':jid,'version':VERSION,'source':source,'requirements_sha256':criteria['requirements_sha256'],'vlm_profile':load_quality(),'status':status,'requirements':rows,'metrics':summarize(rows),'created_at':ident,'artifacts':[],'FAKE':True}
                self.store.db.execute('INSERT INTO ReferenceFidelityReports VALUES (?,?,?,?,?,?,?)',(ident,jid,ident,status,str(test.root),canonical(report),ident));self.store.db.commit();return report
        class FixtureWorker(Worker):
            def __init__(self,db,**kw):super().__init__(db,gate_service_factory=test.fixture.Gates,**kw)
            def execute_stage(self,stage,attempt,asset,settings):
                work=Path(attempt['work_dir'])
                if stage in settings.get('reuse_files',{}) or stage in ('PromptPrepared','ReferenceApproved'):return Worker.execute_stage(self,stage,attempt,asset,settings)
                if stage in ('ReferenceGenerated','GeometryGenerated','PBRGenerated'):
                    op={'ReferenceGenerated':'reference','GeometryGenerated':'shape','PBRGenerated':'pbr'}[stage]
                    slot=test.store.db.execute('SELECT rowid FROM '+('ImageReferences' if op=='reference' else 'ImageShapeRuns')+' WHERE job_id=?',(attempt['job_id'],)).fetchone()[0]
                    claim_budget(test.store,asset['phase34_benchmark_id'],op,slot,attempt['id'])
                if stage=='ReferenceGenerated':
                    p=work/'reference.png';Image.new('RGB',(256,256),(settings['seed']%255,80,100)).save(p);return {'reference':p}
                if stage=='ReferenceProcessed':
                    p=work/'preprocessed.png';im=Image.open(self.path('ReferenceGenerated','reference')).convert('RGBA');alpha=Image.new('L',im.size);ImageDraw.Draw(alpha).rectangle((45,40,210,215),fill=255);im.putalpha(alpha);im.save(p);return {'preprocessed':p}
                if stage=='GeometryGenerated':p=work/'geometry.glb';trimesh.creation.box(extents=(4,3,5)).export(p);return {'geometry':p}
                keys={'GeometryOptimized':['optimized','optimization_report'],'PBRGenerated':['pbr','albedo','metallic','roughness'],'Scaled':['model','scale_report'],'BlenderExported':['blend','blender_report','fbx','obj','lod1','lod2'],'PreviewsRendered':['preview_front','preview_back'],'Validated':['validation'],'Packaged':['manifest.json']}[stage]
                result={}
                for k in keys:
                    p=work/(k+'.json');atomic_json(p,{'FAKE':True,'stage':stage});result[k]=p
                return result
        self.Fidelity=Fidelity;self.Worker=FixtureWorker
        self.verify_patch=patch.object(ImageGenerationProvider,'verify',return_value={'status':'PASS','FAKE':True});self.verify_patch.start()
        self.service=ImageBenchmarkService(self.db,self.store,worker_factory=FixtureWorker,gate_factory=self.fixture.Gates,fidelity_factory=Fidelity,model_check=lambda:True,monitor_gpu=False)
    def tearDown(self):self.verify_patch.stop();self.fixture.tearDown()
    def refs(self,providers=None):return self.service.run('DemoCabin',providers or ['dreamshaper8','z-image-turbo'])
    def shapes(self):
        def fake(service,jid,prepared_evaluation=None,**kw):
            row=prepared_evaluation;r={'evaluation_id':row['id'],'job_id':jid,'decision':'NeedsReview','scores':{'prompt_fidelity':50},'overall_score':50,'FAKE':True};service.store.finish(row['id'],r);return r
        with patch('ai3d_manager.quality_service.QualityService.evaluate',new=fake):return self.service.shapes()
    def test_01_provider_discovery(self):self.assertEqual(set(self.registry['benchmark_providers']),{'dreamshaper8','qwen-image-2512','z-image-turbo'})
    def test_02_provider_configuration(self):
        self.assertEqual(provider_id(self.asset,{'image_provider':'z-image-turbo'}),'z-image-turbo')
        with self.assertRaises(ValueError):provider_id(self.asset,{'image_provider':'imaginary'})
        r=copy.deepcopy(self.registry);r['category_overrides']['Buildings']='z-image-turbo';self.assertEqual(provider_id(self.asset,{},r),'z-image-turbo')
        r['category_overrides']['Towers']='qwen-image-2512';self.assertEqual(provider_id(self.db.asset('DemoTower'),{},r),'qwen-image-2512')
    def test_03_model_availability(self):
        r=copy.deepcopy(self.registry);r['providers']['dreamshaper8']['files'][0]['path']=str(self.root/'absent')
        self.assertEqual(REAL_VERIFY(ImageGenerationProvider('dreamshaper8',r),cache_root=self.root/'verify')['status'],'BLOCKED')
    def test_04_revision_hash_verification(self):
        p=self.root/'weights';p.write_bytes(b'FAKE model weights');info=file_info(p);r=copy.deepcopy(self.registry);r['providers']['dreamshaper8']['files']=[{**info,'sha256':'0'*64}]
        self.assertEqual(REAL_VERIFY(ImageGenerationProvider('dreamshaper8',r),cache_root=self.root/'verify')['files'][0]['status'],'HashMismatch')
    def test_05_quantization_metadata(self):self.assertIn('FP8',self.registry['providers']['qwen-image-2512']['quantization']);self.assertIn('BF16',self.registry['providers']['z-image-turbo']['quantization'])
    def test_06_workflow_schema_validation(self):
        graph=ImageGenerationProvider('z-image-turbo').graph('FAKE',{'optimized':'test','negative':'test'},1)
        with self.assertRaises(ValueError):validate_graph(graph,{})
        self.assertTrue(any(n['class_type']=='ConditioningZeroOut' for n in graph.values()))
    def test_07_provider_prompt_adaptation(self):
        for p in self.registry['providers'].values():
            r=adapt_prompt(self.asset,p,'model-optimized');self.assertIn(self.asset['prompt'],r['positive']);self.assertEqual(r['style'],self.asset['shared_style'])
    def test_08_controlled_prompt_consistency(self):
        values=[adapt_prompt(self.asset,self.registry['providers'][p]) for p in self.registry['benchmark_providers']]
        self.assertEqual(len({r['positive'] for r in values}),1);self.assertEqual(len({r['source_sha256'] for r in values}),1)
    def test_09_reference_candidate_isolation(self):
        self.refs();rows=ref_rows(self.store,self.service.prepare(['dreamshaper8','z-image-turbo'])['id']);self.assertEqual(len(rows),4);self.assertEqual(len({r['job_id'] for r in rows}),4)
    def test_10_alpha_preprocessing(self):
        self.refs();jid=ref_rows(self.store,self.service.prepare(['dreamshaper8','z-image-turbo'])['id'])[0]['job_id'];im=Image.open(outputs_for(self.db,jid)['ReferenceProcessed']['preprocessed']['path']);self.assertIn('A',im.getbands());self.assertLess(im.getchannel('A').getextrema()[0],255)
    def test_11_gate_integration(self):
        r=self.refs();self.assertTrue(all(i['gate_status']=='PASS' for i in r['profiles']['controlled']))
    def test_12_fidelity_extraction(self):
        r=extract(self.asset);self.assertTrue(any(i['type']=='unverifiable' and not i['visually_evaluable'] for i in r['requirements']));self.assertTrue(any('pitched metal roof' in i['source']['quote'] for i in r['requirements']))
    def test_13_fidelity_criterion_validation(self):
        r=extract(self.asset);self.assertEqual(set(schema_for(r['requirements'])['properties']['criteria']['required']),{i['id'] for i in r['requirements'] if i['visually_evaluable']})
        self.assertEqual(schema_for(r['requirements'])['properties']['summary']['maxLength'],600)
    def test_14_evidence_consistency(self):
        c=extract(self.asset)['requirements'];v={'criteria':{r['id']:{'result':'Uncertain','evidence':'FAKE','confidence':.5,'related_images':['reference'],'uncertainty_reason':''} for r in c if r['visually_evaluable']}}
        with self.assertRaises(ValueError):validate_evidence(v,c)
    def test_15_candidate_scoring(self):
        rows=[{'visually_evaluable':True,'importance':'critical','result':'Unmet','confidence':.9,'id':'R1','category':'prompt_fidelity'}]
        self.assertEqual(fidelity_decision(rows),'FAIL');self.assertEqual(summarize(rows)['requirement_fidelity'],0)
    def test_16_candidate_ranking(self):
        a={'gate_status':'PASS','fidelity_status':'PASS','fidelity_metrics':{'major_unmet':['a'],'requirement_fidelity':90}}
        b={'gate_status':'PASS','fidelity_status':'PASS','fidelity_metrics':{'major_unmet':[],'requirement_fidelity':80}}
        self.assertGreater(rank_key(b),rank_key(a))
        from ai3d_manager.image_benchmark import choose_profile_reference
        a.update(provider_id='same',eligible_for_shape=True);b.update(provider_id='same',eligible_for_shape=True)
        self.assertEqual(choose_profile_reference(b,[a]),b)
    def test_17_cross_provider_comparison(self):
        r=self.refs();items=r['profiles']['controlled'];self.assertEqual({i['provider_id'] for i in items},{'dreamshaper8','z-image-turbo'});self.assertEqual(len({i['fidelity_protocol'] for i in items}),1)
    def test_18_model_optimized_separation(self):
        self.refs();r=self.service.run('DemoCabin',['dreamshaper8','z-image-turbo'],profile='model-optimized');self.assertEqual(set(r['profiles']),{'controlled','model-optimized'});self.assertEqual(len(r['profiles']['model-optimized']),2)
    def test_19_gpu_sequencing(self):
        from ai3d_manager.quality_vlm import check_vlm_reservation
        p=self.root/'reservation.json';atomic_json(p,{'FAKE':True})
        with self.assertRaises(ProductionError):check_vlm_reservation(p)
        from ai3d_manager.image_providers import DualComfyQualityLease
        from tests.test_quality import Response
        class Session:
            def get(self,url,**kwargs):return Response({'queue_running':[[1,'foreign']] if ':8191/' in url else [],'queue_pending':[]})
        class VLM:session=Session();url='http://127.0.0.1:11434'
        lease=DualComfyQualityLease(VLM(),self.root)
        with self.assertRaises(ProductionError):lease.queue_empty()
    def test_20_disk_budget(self):
        self.refs();p=self.service.prepare(['dreamshaper8','z-image-turbo']);p['disk_budget_bytes']=1
        with self.assertRaises(ProductionError):budget_boundary(self.db,self.store,p)
    def test_21_inference_budget(self):
        with self.assertRaises(ValueError):self.service.prepare(['dreamshaper8']*5)
        self.refs();p=self.service.prepare(['dreamshaper8','z-image-turbo']);claim_budget(self.store,p['id'],'shape',1,'first')
        with self.assertRaises(ProductionError):claim_budget(self.store,p['id'],'shape',1,'retry')
    def test_22_hunyuan_compatibility(self):
        self.refs(self.registry['benchmark_providers']);r=self.shapes();self.assertEqual(len(r['runs']),2);self.assertEqual(r['different_providers'],2)
        for run in r['runs']:self.assertEqual(json.loads(self.db.job(run['job_id'])['settings_json'])['shape_resolution'],384)
    def test_23_geometry_gate_integration(self):
        self.refs(self.registry['benchmark_providers']);self.fixture.gate_states['geometry']='FAIL';r=self.shapes();self.assertTrue(all(run['geometry_gate']['status']=='FAIL' for run in r['runs']));self.assertFalse(any(s['name']=='PBRGenerated' for run in r['runs'] for s in self.db.stages(run['job_id'])))
        from ai3d_manager.image_benchmark import model_comparison
        report=model_comparison(self.db,self.store,'DemoCabin');self.assertEqual(report['status'],'BLOCKED');self.assertEqual(report['completed_evaluations'],0)
    def test_24_pbr_after_identity(self):
        self.refs(self.registry['benchmark_providers']);r=self.shapes();self.assertTrue(all('PBRGenerated' in outputs_for(self.db,run['job_id']) for run in r['runs']))
    def test_25_existing_job_preservation(self):
        self.refs();before={str(p):file_info(p) for p in self.db.output_root.rglob('*') if p.is_file()};self.refs()
        self.assertTrue(all(intact(i) for i in before.values()))
    def test_26_benchmark_persistence(self):
        self.refs();self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ImageBenchmarks').fetchone()[0],1)
    def test_27_restart_recovery(self):
        self.refs();other=ImageBenchmarkService(self.db,self.store,model_check=lambda:True);self.assertEqual(other.prepare(['dreamshaper8','z-image-turbo'])['id'],self.service.prepare(['dreamshaper8','z-image-turbo'])['id'])
    def test_28_idempotency(self):
        self.refs();before=len(self.db.db.execute('SELECT * FROM JobAttempts').fetchall());self.refs();self.assertEqual(before,len(self.db.db.execute('SELECT * FROM JobAttempts').fetchall()))
    def test_29_category_profiles(self):
        from ai3d_manager.gate_analysis import profile_for
        expected={'DemoTower':'tower','DemoTree':'tree','DemoPile':'debris'}
        for ident,name in expected.items():self.assertEqual(profile_for(self.db.asset(ident))['name'],name)
        with self.assertRaises(ValueError):self.service.run('Tree')
    def test_30_backward_compatibility(self):
        from ai3d_manager.comfy import graphs
        p=self.registry['providers']['dreamshaper8'];prompt={'optimized':'FAKE','negative':'FAKE'}
        old=graphs('ReferenceGenerated','FAKE',{'checkpoint':p['checkpoint'],'seed':42,'image_size':512,'image_steps':25},prompt)
        self.assertEqual(ImageGenerationProvider('dreamshaper8').graph('FAKE',prompt,42),old)
        r=copy.deepcopy(self.registry);r['default_provider']='z-image-turbo'
        self.assertEqual(provider_id({**self.asset,'phase33_set_id':'historical'},{},r),'dreamshaper8')


if __name__=='__main__':unittest.main(verbosity=2)
