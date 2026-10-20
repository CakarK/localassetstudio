"""Phase 3 unit/contract tests; fake VLM outputs are never real visual evidence."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from tests.runtime import TEST_ROOT
from unittest.mock import patch
from PIL import Image
from ai3d_manager.catalog import load_catalog
from ai3d_manager.common import AI, ProductionError, atomic_json, canonical, digest, file_info, intact
from ai3d_manager.database import Database
from ai3d_manager.cli import main
from ai3d_manager.worker import settings_for
from ai3d_manager.quality_requirements import requirements,classify
from ai3d_manager.quality_schema import parse_response,response_schema,EXTRACTION
from ai3d_manager.quality_scoring import score,WEIGHTS,policy
from ai3d_manager.quality_service import map_renders,VIEWS,validate_config
from ai3d_manager.quality_store import QualityStore
from ai3d_manager.quality_planner import retry_policy,enqueue_candidates,plan_corrections,save_plan,compare_candidates,BudgetedWorker
from ai3d_manager.quality_vlm import OllamaVLM,GPUQualityLease,local_url,check_vlm_reservation,resolve_reservation

class Response:
    ok=True;status_code=200
    def __init__(self,data):self.data=data
    def json(self):return self.data
    def raise_for_status(self):pass

def fixture_payload(interpretation,value=90):
    return {'scores':{c:{'score':value,'confidence':.95,'views':VIEWS,'reason':'FAKE test fixture'} for c in WEIGHTS},
        'observations':[{'criterion_id':r['id'],'result':'met','confidence':.95,'views':['front','back'],
                         'evidence':'FAKE test evidence','severity':'info','suggested_fix':'None required in fake fixture'} for r in interpretation['requirements'] if r['visually_evaluable']],
        'contradictions':[],'requested_detail_views':[]}

class QualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.asset=next(a for a in load_catalog() if a['asset_id']=='DemoCabin')
    def setUp(self):
        # The original Phase 3 tests isolate scoring/CLI behavior. New mandatory
        # gates are covered without this mock in test_phase32.py.
        self.gate_patch=patch('ai3d_manager.quality_gates.gate_summary',return_value={'status':'PASS','reference':{'status':'PASS'},'geometry':{'status':'PASS'}})
        self.gate_patch.start()
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name)
        self.db=Database(self.root/'jobs.sqlite',self.root/'outputs');self.db.sync_assets([self.asset])
        self.store=QualityStore(self.root/'quality.sqlite',self.root/'quality')
        self.j=self.db.enqueue(self.asset,'001',settings_for(self.asset))[0]
        self.db.status(self.j['id'],'Completed');self.j=self.db.job(self.j['id'])
        self.i=requirements(self.asset);self.payload=fixture_payload(self.i)
        self.technical={'status':'WARN','checks':[{'check':'footprint','status':'PASS','detail':{'size':[8,8]}}]}
        self.renders=[{'view':v,'width':768,'height':768,'mode':'material','vlm_width':512} for v in VIEWS]
    def tearDown(self):
        self.gate_patch.stop()
        import time
        self.store.close();self.db.close()
        # Only this test's freshly allocated directory may be removed. OneDrive
        # can create short sharing/cleanup races after SQLite files are closed.
        assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for attempt in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if attempt==7:raise
                time.sleep(.05*(attempt+1))
    def result(self,payload=None,technical=None,config=None):
        return score(payload or self.payload,self.i,technical or self.technical,self.renders,config)
    def save(self,report=None,job=None):
        job=job or self.j;e=self.store.begin(job,{})
        r=report or self.result();r.update(job_id=job['id'],asset_id=job['asset_id'],evaluation_id=e['id'],evaluation_version='test-1',model_version='FAKE',interpretation_sha256=self.i['interpretation_sha256'])
        self.store.finish(e['id'],r);return r
    def test_parse_valid_response(self):
        r={'done':True,'done_reason':'stop','message':{'content':json.dumps(self.payload)}}
        self.assertEqual(parse_response(r,response_schema(self.i['requirements'],VIEWS)),self.payload)
    def test_single_json_fence_and_positive_empty_fix(self):
        p=copy.deepcopy(self.payload);p['observations'][0]['suggested_fix']=''
        r={'done':True,'done_reason':'stop','message':{'content':'```json\n'+json.dumps(p)+'\n```'}}
        self.assertEqual(parse_response(r,response_schema(self.i['requirements'],VIEWS)),p)
        r['message']['content']='Here is the JSON:\n'+r['message']['content']
        with self.assertRaises(ValueError):parse_response(r,response_schema(self.i['requirements'],VIEWS))
    def test_schema_invalid_unknown_duplicate_nonfinite(self):
        for mutate in (lambda p:p['scores']['geometry'].update(score=101),lambda p:p['observations'][0].update(views=['fictional_view']),
                       lambda p:p['observations'][1].update(criterion_id=p['observations'][0]['criterion_id']),
                       lambda p:p.update(extra='unrecognized')):
            p=copy.deepcopy(self.payload);mutate(p)
            with self.assertRaises(Exception):parse_response({'done':True,'message':{'content':json.dumps(p)}},response_schema(self.i['requirements'],VIEWS))
        with self.assertRaises(ValueError):parse_response({'done':True,'message':{'content':'NaN'}},{})
    def test_truncated_and_thinking_rejected(self):
        for response in ({'done':False},{'done':True,'done_reason':'length'},{'done':True,'message':{'content':'<think>test</think>'}}):
            with self.assertRaises(ValueError):parse_response(response,{})
    def test_requirement_source_anchors_and_version(self):
        self.assertEqual(requirements(self.asset)['interpretation_sha256'],self.i['interpretation_sha256'])
        for r in self.i['requirements']:
            source=r['source'];self.assertEqual(self.asset[source['field']][source['start']:source['end']],source['quote'])
        ai={'requirements':[{'description':'invented','quote':'moon rocket','field':'prompt','type':'visual','importance':'major'}]}
        with self.assertRaises(ValueError):requirements(self.asset,ai)
    def test_requirement_classification_no_pixel_measurement(self):
        self.assertEqual(classify('3 x 3 meters footprint'),'technical');self.assertEqual(classify('capacity for two people'),'unverifiable')
        r=self.i['requirements'];self.assertTrue(any(x['type']=='unverifiable' and not x['visually_evaluable'] for x in r));self.assertTrue(any(x['type']=='technical' and not x['visually_evaluable'] for x in r))
        all_assets=load_catalog();self.assertTrue(all(requirements(a)['requirements'] for a in all_assets))
    def test_weighted_score_and_custom_weights(self):
        p=copy.deepcopy(self.payload)
        for n,c in enumerate(WEIGHTS):p['scores'][c]['score']=n*20
        r=self.result(p);expected=sum(n*20*w for n,w in enumerate(WEIGHTS.values()));self.assertAlmostEqual(r['overall_score'],expected)
        custom={c:0 for c in WEIGHTS};custom['prompt_fidelity']=1
        self.assertEqual(self.result(p,config={'weights':custom})['overall_score'],0)
        with self.assertRaises(ValueError):policy({'weights':{'geometry':1}})
    def test_confidence_and_unseen_absence(self):
        self.i['requirements'][0]['importance']='critical'
        p=copy.deepcopy(self.payload);p['observations'][0].update(result='not_met',views=['front'],severity='critical')
        r=self.result(p);self.assertEqual(r['decision'],'NeedsReview');self.assertFalse(r['critical_findings'])
        p=copy.deepcopy(self.payload);p['scores']['texture']['confidence']=.3
        self.assertEqual(self.result(p)['decision'],'NeedsReview')
    def test_unverifiable_requirement_not_promoted(self):
        r=self.result();nv=next(x for x in r['requirements'] if x['type']=='unverifiable')
        self.assertEqual(nv['result'],'UnverifiableFromRenders');self.assertIsNone(nv['confidence'])
        tm=next(x for x in r['requirements'] if x['type']=='technical');self.assertEqual(tm['result'],'PASS')
    def test_critical_and_technical_failure_gate(self):
        p=copy.deepcopy(self.payload);p['observations'][0].update(result='not_met',severity='critical')
        self.assertEqual(self.result(p)['decision'],'RejectedCandidate')
        r=self.result(technical={'status':'FAIL','checks':[]});self.assertEqual(r['decision'],'NeedsReview');self.assertEqual(r['technical_status'],'TechnicalFailed')
    def test_conflicting_evidence_and_resolution(self):
        p=copy.deepcopy(self.payload);p['contradictions']=['front and back disagree'];self.assertEqual(self.result(p)['decision'],'NeedsReview')
        self.renders[0]['vlm_width']=256;self.assertEqual(self.result()['decision'],'NeedsReview')
    def test_shadow_mode_and_existing_job_unchanged(self):
        before=self.db.job(self.j['id']);r=self.save()
        self.assertEqual(r['decision'],'VisuallyApprovedCandidate');self.assertEqual(self.db.job(self.j['id']),before)
        with self.assertRaises(ValueError):policy({'mode':'enforce'})
    def test_approval_audit_and_persistence(self):
        self.save();self.store.action(self.j['id'],'Approved','Human checked all six views');self.store.action(self.j['id'],'Rejected','Second reviewer found platform')
        actions=self.store.actions(self.j['id']);self.assertEqual([a['action'] for a in actions],['Approved','Rejected']);self.assertTrue(all(a['created_at'] and a['reason'] for a in actions))
        with self.assertRaises(ValueError):self.store.action(self.j['id'],'Approved',' ')
        self.store.close();self.store=QualityStore(self.root/'quality.sqlite');self.assertEqual(len(self.store.actions(self.j['id'])),2)
    def test_cli_manual_review_and_report(self):
        self.save();prefix=['--db',str(self.db.path),'--quality-db',str(self.store.path),'--quality-root',str(self.root/'quality'),'quality']
        before=self.db.job(self.j['id'])
        for args in (['report',self.j['id']],['approve',self.j['id'],'--reason','Fake CLI contract test'],['reject',self.j['id'],'--reason','Fake review'],['compare','DemoCabin'],['suggest-fixes',self.j['id']]):
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(prefix+args),0)
        self.assertEqual(before,self.db.job(self.j['id']))
    def test_candidate_comparison_excludes_critical_and_fail(self):
        r=self.save();self.assertEqual(compare_candidates(self.db,self.store,'DemoCabin')['recommended_job_id'],self.j['id'])
        r['critical_findings']=[{'test':True}];self.save(r)
        self.assertIsNone(compare_candidates(self.db,self.store,'DemoCabin')['recommended_job_id'])
    def test_different_protocols_not_comparable(self):
        self.save();other=self.db.enqueue(self.asset,'002',settings_for(self.asset))[0]
        r=self.result();r['policy']={'test':'different'};self.save(r,other)
        report=compare_candidates(self.db,self.store,'DemoCabin');self.assertFalse(report['same_evaluation_protocol']);self.assertIsNone(report['recommended_job_id'])
    def test_correction_plan_requires_review_for_platform(self):
        r=self.save();r['findings']=[{'criterion_id':'GE01','category':'geometry','problem':'Integrated ground platform','evidence_images':['front','back'],'confidence':.95,'suggested_fix':'Separate only after human mesh review'}]
        plan=plan_corrections(r);self.assertFalse(plan['actions'][0]['automatic_execution_supported']);self.assertFalse(plan['automatic_retry']['enabled'])
    def test_retry_limit_disabled_and_texture_reuse(self):
        with self.assertRaises(ValueError):retry_policy({'max_candidates':4})
        # Tiny checkpoint files are fake; prove stage selection without inference.
        for stage,key in [('ReferenceGenerated','reference'),('ReferenceProcessed','preprocessed'),('GeometryGenerated','geometry'),('GeometryOptimized','optimized')]:
            attempt=self.db.begin_stage(self.j['id'],stage,'fake',{},{});path=Path(attempt['work_dir'])/'fake.txt';path.write_text('FAKE checkpoint')
            self.db.complete_stage(attempt,{key:file_info(path)})
        r=self.save();r['findings']=[{'criterion_id':'TX01','category':'texture','problem':'blurry texture','evidence_images':['left','right'],'confidence':.9,'suggested_fix':'Repaint texture'}]
        plan=save_plan(self.store,r)
        with self.assertRaises(ProductionError):enqueue_candidates(self.db,self.store,plan)
        prepared=enqueue_candidates(self.db,self.store,plan,{'enabled':True,'max_candidates':3,'max_disk_mb':1})
        self.assertEqual(len(prepared['candidates']),3);self.assertFalse(prepared['inference_started'])
        seeds=[]
        for c in prepared['candidates']:
            job=self.db.job(c['job_id']);s=json.loads(job['settings_json']);seeds.append(s['seed'])
            self.assertEqual(set(s['reuse_files']),{'ReferenceGenerated','ReferenceProcessed','GeometryGenerated','GeometryOptimized'})
            self.assertNotEqual(job['output_dir'],self.j['output_dir']);self.assertEqual(job['status'],'Pending')
        self.assertEqual(len(set(seeds)),3)
        self.assertEqual(len(enqueue_candidates(self.db,self.store,plan,{'enabled':True,'max_disk_mb':1})['candidates']),3)
    def test_gpu_disk_budgets_boundary(self):
        import time
        w=BudgetedWorker(self.db,time.monotonic()-1,1024,allow_gpu=False)
        with self.assertRaises(ProductionError):w.boundary(self.j['id'])
        with self.assertRaises(ValueError):retry_policy({'max_gpu_seconds':0})
        w=BudgetedWorker(self.db,time.monotonic()+100,1,allow_gpu=False)
        with self.assertRaises(ProductionError) as error:w.boundary(self.j['id'])
        self.assertEqual(error.exception.category,'DiskBudget')
        with self.assertRaises(ValueError):retry_policy({'max_gpu_seconds':float('nan')})
    def test_invalid_config_rejected_before_render_or_gpu(self):
        for config in ({'unknown':True},{'allow_cpu_fallback':'false'},{'resolution':-1},{'vlm_timeout':float('inf')},{'diagnostics':['invalid']}):
            with self.assertRaises(ValueError):validate_config(config)
    def test_render_mapping_and_tamper(self):
        directory=self.root/'renders';directory.mkdir();items=[]
        for v in VIEWS:
            path=directory/(v+'.png');Image.new('RGB',(768,768),'gray').save(path)
            items.append({'view':v,'mode':'material',**file_info(path)})
        atomic_json(directory/'render-metadata.json',{'source_sha256':'mesh-hash','artifacts':items})
        _,artifacts=map_renders(directory,{'sha256':'mesh-hash'});self.assertEqual(len(artifacts),6)
        (directory/'front.png').write_text('bad')
        with self.assertRaises(ProductionError):map_renders(directory,{'sha256':'mesh-hash'})
    def test_gpu_sequencing_busy_never_unloads_foreign(self):
        class HTTP:
            def get(self,url,**kwargs):return Response({'queue_running':[[1,'foreign']], 'queue_pending':[]})
            def post(self,*a,**k):raise AssertionError('Must not unload or infer')
        vlm=OllamaVLM(session=HTTP())
        with self.assertRaises(ProductionError) as error:
            with GPUQualityLease(vlm,self.root,lock_path=self.root/'isolated-production.lock'):raise AssertionError('Busy queue must block')
        self.assertEqual(error.exception.category,'GPUBusy')
    def test_local_only_no_download(self):
        with self.assertRaises(ValueError):local_url('https://cloud.example.com')
        class HTTP:
            def get(self,url,**kw):return Response({'version':'test'} if url.endswith('/version') else {'models':[]})
            def post(self,*a,**kw):raise AssertionError('No pull/download request allowed')
        with self.assertRaises(ProductionError):OllamaVLM(session=HTTP()).metadata()
    def test_resident_foreign_model_blocks_unload(self):
        class HTTP:
            def get(self,url,**kwargs):
                return Response({'queue_running':[],'queue_pending':[]} if url.endswith('/queue') else {'models':[{'name':'foreign-model'}]})
            def post(self,*a,**k):raise AssertionError('Foreign model must not be unloaded')
        with self.assertRaises(ProductionError) as error:
            with GPUQualityLease(OllamaVLM(session=HTTP()),self.root,lock_path=self.root/'lease.lock'):pass
        self.assertEqual(error.exception.category,'GPUBusy')
    def test_insufficient_vram_and_oom_fail_closed(self):
        from ai3d_manager.common import ProcessLock
        class HTTP:
            def get(self,url,**kwargs):
                if url.endswith('/queue'):return Response({'queue_running':[],'queue_pending':[]})
                if url.endswith('/ps'):return Response({'models':[]})
                return Response({'system':{'argv':[str(AI/'ComfyUI/main.py')]}})
            def post(self,*a,**k):return Response({})
        lock=self.root/'lease.lock'
        with patch('ai3d_manager.quality_vlm.time.sleep'):
            with self.assertRaises(ProductionError) as error:
                with GPUQualityLease(OllamaVLM(session=HTTP()),self.root,lock_path=lock,sampler=lambda:{'free_mib':1000}):pass
        self.assertEqual(error.exception.category,'InsufficientVRAM')
        with ProcessLock(lock):pass
        class OOM:
            def post(self,*a,**k):
                r=Response({});r.ok=False;r.text='CUDA out of memory';return r
        with self.assertRaises(ProductionError) as error:OllamaVLM(session=OOM()).structured('test',EXTRACTION,self.root,'oom')
        self.assertEqual(error.exception.category,'CUDA_OOM')
    def test_owned_gpu_release_and_uncertain_request_reservation(self):
        class HTTP:
            def get(self,url,**kwargs):
                if url.endswith('/queue'):return Response({'queue_running':[],'queue_pending':[]})
                if url.endswith('/ps'):return Response({'models':[]})
                return Response({'system':{'argv':[str(AI/'ComfyUI/main.py')]}})
            def post(self,*a,**k):return Response({})
        http=HTTP();vlm=OllamaVLM(session=http);lock=self.root/'lease.lock'
        with GPUQualityLease(vlm,self.root,lock_path=lock,sampler=lambda:{'free_mib':22000}) as lease:
            lease.mark_owned();self.assertTrue(lease.marker.exists())
        self.assertFalse(lease.marker.exists())
        with self.assertRaises(ProductionError):
            with GPUQualityLease(vlm,self.root,lock_path=lock,sampler=lambda:{'free_mib':22000}) as lease:
                lease.mark_owned();raise ProductionError('VLMHTTP','Timeout may still be running',uncertain=True)
        self.assertTrue(lease.marker.exists())
        with self.assertRaises(ProductionError):check_vlm_reservation(lease.marker)
        resolve_reservation('FAKE operator confirmed request finished',session=http,path=lease.marker)
        self.assertFalse(lease.marker.exists())
    def test_schema_repair_bounded(self):
        class HTTP:
            calls=0
            def post(self,*a,**kw):self.calls+=1;return Response({'done':True,'message':{'content':'invalid json'}})
        http=HTTP();vlm=OllamaVLM(session=http)
        with self.assertRaises(ProductionError):vlm.structured('test',EXTRACTION,self.root,'fake')
        self.assertEqual(http.calls,2);self.assertTrue((self.root/'fake-invalid.json').exists())
    @unittest.skipUnless((AI/'Backups/phase_3/preserved-outputs.json').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_technical_validator_and_original_outputs_unchanged(self):
        baseline=json.loads((AI/'Backups/phase_3/preserved-outputs.json').read_text('utf-8'))
        for path,info in baseline.items():self.assertTrue(intact({'path':path,**info}),path)
        self.assertEqual(self.db.job(self.j['id'])['status'],'Completed')

if __name__=='__main__':unittest.main(verbosity=2)
