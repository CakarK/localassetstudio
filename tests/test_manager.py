"""Fake-service contract tests. Real Blender/GPU evidence is reported separately."""
import contextlib
import ast
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from tests.runtime import TEST_ROOT
from unittest.mock import patch
import requests
import trimesh
import numpy as np
from PIL import Image
from ai3d_manager.catalog import load_catalog
from ai3d_manager.cli import main
from ai3d_manager.common import AI, ProductionError, ProcessLock, atomic_json, canonical, file_info, intact
from ai3d_manager.database import Database
from ai3d_manager.comfy import ComfyClient, classify, graphs
from ai3d_manager.worker import Worker, STAGES, settings_for
from ai3d_manager.mesh_ops import optimize, scale
from ai3d_manager.validation import validate
from ai3d_manager.processes import run_process
from ai3d_manager.prompts import prepare, guard


class FakeWorker(Worker):
    """Produces marked text artifacts only; not a geometry or inference simulation."""
    def before_stage(self,*args):
        # These legacy orchestration fixtures contain text, not reference/mesh
        # evidence. Phase 3.2 tests exercise mandatory gates with real CPU fixtures.
        pass
    def __init__(self,db,fail=None):super().__init__(db);self.calls=[];self.fail=fail
    def execute_stage(self,stage,attempt,asset,settings):
        self.calls.append(stage)
        if stage==self.fail:raise ProductionError('BlenderExecution','injected failure')
        p=Path(attempt['work_dir'])/'fake.json'
        atomic_json(p,{'fake':True,'stage':stage,'parameters':self.stage_parameters(stage,asset,settings),
                       'inputs':{d:{k:v['sha256'] for k,v in self.results[d].items()} for d in self.results}})
        return {'fake':p}


class Response:
    ok=True;status_code=200;text=''
    def __init__(self,data):self.data=data
    def json(self):return self.data
    def raise_for_status(self):pass


class FakeHTTP:
    def __init__(self,lose=False,missing=False):self.posts=0;self.hist={};self.lose=lose;self.missing=missing
    def get(self,url,**kwargs):
        if url.endswith('/queue'):return Response({'queue_running':[],'queue_pending':[]})
        return Response({} if self.missing else self.hist)
    def post(self,url,json=None,**kwargs):
        self.posts+=1
        self.hist={'pid1':{'status':{'status_str':'success'},'outputs':{},'prompt':[0,'pid1',{}, {'client_id':json['client_id']}]}}
        if self.lose:raise requests.ConnectionError('injected lost POST response')
        return Response({'prompt_id':'pid1'})


class ManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog=load_catalog();cls.asset=next(a for a in cls.catalog if a['asset_id']=='DemoCabin')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name)
        self.db=Database(self.root/'jobs.sqlite',self.root/'outputs');self.db.sync_assets(self.catalog)
    def tearDown(self):
        import time
        self.db.close()
        assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for attempt in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if attempt==7:raise
                time.sleep(.05*(attempt+1))
    def job(self,variant='001',**settings):return self.db.enqueue(self.asset,variant,settings_for(self.asset,settings))[0]
    def test_synthetic_catalog_unique(self):
        self.assertEqual(len(self.catalog),8);self.assertEqual(len({a['asset_id'] for a in self.catalog}),8)
        self.assertEqual(self.asset['period'],'demo');self.assertEqual(self.asset['category'],'Buildings')
    def test_grid_and_budget(self):
        grids=set()
        for a in self.catalog:
            settings_for(a)
            if a['grid']:self.assertEqual(a['footprint_m'],[a['grid_cell_m']*v for v in a['grid']]);grids.add(tuple(a['grid']))
        self.assertTrue({(1,1),(2,2),(3,2),(3,3)}<=grids)
        self.assertEqual(self.asset['polygon_range'],[3600,14400])
    def test_overrides_provenance(self):
        a=load_catalog(overrides={'DemoCabin':{'grid':[3,2],'polygon_target':11000}})
        a=next(x for x in a if x['asset_id']=='DemoCabin')
        self.assertEqual(a['footprint_m'],[4.5,3]);self.assertEqual(a['origins']['polygon_target'],'explicit_override')
        self.assertTrue(any('configured_default' in x['origins']['polygon_budget'] for x in self.catalog))
    def test_prompt_english_bypass_and_negative(self):
        with patch.object(guard,'translate_prompt',side_effect=AssertionError('must bypass')):
            p=prepare(self.asset,settings_for(self.asset))
        self.assertIn(self.asset['shared_style'],p['optimized']);self.assertEqual(p['negative'],self.asset['shared_negative'])
        g=graphs('ReferenceGenerated','GM_a',settings_for(self.asset),p)
        self.assertEqual(g['3']['inputs']['text'],p['negative'])
    def test_translation_rejection(self):
        for content,reason in [('<think>reason</think>','stop'),('{"prompt":"Here is the translated hut"}','stop'),
            ('{"prompt":""}','stop'),('{"prompt":"wooden hut,"}','stop'),('{"prompt":"wooden hut"}','length'),('[]','stop')]:
            with self.subTest(content=content),self.assertRaises(guard.PromptTranslationError):
                guard.validate_translation({'done':True,'done_reason':reason,'message':{'content':content}})
    def test_translation_limited_retry(self):
        calls=[]
        def post(*a,**kw):calls.append(1);return Response({'done':True,'message':{'content':'bad'}})
        with self.assertRaises(guard.PromptTranslationError):guard.translate_prompt('kulübe',post=post)
        self.assertEqual(len(calls),2)
        self.assertEqual(guard.validate_translation({'done':True,'done_reason':'stop','message':{'content':'{"prompt":"A weathered wooden hut"}'}}),'A weathered wooden hut')
    def test_idempotency_versions(self):
        j=self.job();other=self.job();self.assertEqual(j['id'],other['id'])
        new,created=self.db.enqueue(self.asset,'001',settings_for(self.asset),True)
        self.assertTrue(created);self.assertEqual(new['version'],2);self.assertNotEqual(j['output_dir'],new['output_dir'])
    def test_queue_priority_and_persistence(self):
        j=self.job();other,_=self.db.enqueue(self.asset,'high',settings_for(self.asset),priority=10)
        self.assertEqual(self.db.next_job()['id'],other['id'])
        self.db.status(other['id'],'GeometryGenerated');self.assertEqual(self.db.next_job()['id'],other['id'])
        path=self.db.path;self.db.close();self.db=Database(path,self.root/'outputs')
        self.assertEqual(self.db.job(j['id'])['status'],'Pending');self.assertEqual(len(self.db.jobs()),2)
    def test_pending_cancel_and_pause(self):
        j=self.job();self.db.cancel(j['id']);self.assertEqual(self.db.job(j['id'])['status'],'Cancelled')
        j=self.job('pause');self.db.set_setting('queue_paused',True)
        w=FakeWorker(self.db);self.assertEqual(w.run(j['id'])['status'],'Paused');self.assertFalse(w.calls)
        self.db.set_setting('queue_paused',False);self.assertEqual(w.run(j['id'])['status'],'Completed')
    def test_missing_enqueue_snapshot_recovered_and_tamper_rejected(self):
        j=self.job();request=Path(j['output_dir'])/'request.json';request.unlink()
        self.assertEqual(FakeWorker(self.db).run(j['id'])['status'],'Completed');self.assertTrue(request.exists())
        request.write_text('{}');self.db.resume(j['id']);w=FakeWorker(self.db)
        self.assertEqual(w.run(j['id'])['status'],'Failed');self.assertFalse(w.calls)
    def test_safe_running_cancel(self):
        j=self.job();w=FakeWorker(self.db);original=w.execute_stage
        def execute(stage,*args):
            result=original(stage,*args);self.db.cancel(j['id']);return result
        w.execute_stage=execute
        self.assertEqual(w.run(j['id'])['status'],'Cancelled');self.assertEqual(w.calls,['PromptPrepared'])
    def test_fail_resume_only_required_stages(self):
        j=self.job();w=FakeWorker(self.db,'BlenderExported');self.assertEqual(w.run(j['id'])['status'],'Failed')
        prior=self.db.stages(j['id']);self.db.resume(j['id']);w=FakeWorker(self.db)
        self.assertEqual(w.run(j['id'])['status'],'Completed');self.assertEqual(w.calls,STAGES[8:])
        self.assertTrue(all(row['ended_at'] and row['parameters_json'] for row in prior))
    def test_hash_tamper_and_selective_invalidation(self):
        j=self.job();w=FakeWorker(self.db);w.run(j['id']);old=self.db.stages(j['id'])
        stage=next(s for s in old if s['name']=='PBRGenerated');artifact=self.db.artifact_dict(stage['id'])['fake']
        Path(artifact['path']).write_text('damaged');self.assertFalse(intact(artifact))
        self.db.resume(j['id']);w=FakeWorker(self.db);w.run(j['id'])
        self.assertEqual(w.calls[0],'PBRGenerated');self.assertNotIn('GeometryGenerated',w.calls)
        self.db.resume(j['id'],{'height_m':7.0});w=FakeWorker(self.db);w.run(j['id'])
        self.assertEqual(w.calls[0],'Scaled');self.assertNotIn('PBRGenerated',w.calls)
    def test_completed_resume_no_work(self):
        j=self.job();FakeWorker(self.db).run(j['id']);self.db.resume(j['id']);w=FakeWorker(self.db);w.run(j['id']);self.assertFalse(w.calls)
    def test_process_lock(self):
        with ProcessLock(self.root/'lock'):
            with self.assertRaises(ProductionError):
                with ProcessLock(self.root/'lock'):pass
    def test_post_response_lost_restart_recovery(self):
        j=self.job();a=self.db.begin_stage(j['id'],'GeometryGenerated','fp',{},{});session=FakeHTTP(lose=True)
        c=ComfyClient(session=session,poll=.001)
        with self.assertRaises(ProductionError):c.execute(self.db,a,{}, {'namespace':'GM_test'})
        self.assertEqual(self.db.attempts(j['id'])[0]['state'],'SubmissionUnknown')
        self.db.close();self.db=Database(self.root/'jobs.sqlite',self.root/'outputs')
        recovered=c.execute(self.db,self.db.attempts(j['id'])[0]);self.assertEqual(recovered['status']['status_str'],'success')
        self.assertEqual(session.posts,1);self.assertEqual(self.db.attempts(j['id'])[0]['prompt_id'],'pid1')
    def test_unknown_remote_never_resubmitted(self):
        j=self.job();a=self.db.begin_stage(j['id'],'GeometryGenerated','fp',{},{});self.db.attempt_update(a['id'],state='Submitting')
        session=FakeHTTP(missing=True)
        with self.assertRaises(ProductionError) as error:ComfyClient(session=session).execute(self.db,self.db.attempts(j['id'])[0])
        self.assertTrue(error.exception.uncertain);self.assertEqual(session.posts,0)
    def test_known_prompt_recovery(self):
        j=self.job();a=self.db.begin_stage(j['id'],'PBRGenerated','fp',{},{});http=FakeHTTP();c=ComfyClient(session=http)
        c.execute(self.db,a,{},{});c.execute(self.db,self.db.attempts(j['id'])[0]);self.assertEqual(http.posts,1)
    def test_server_500_is_uncertain(self):
        j=self.job();a=self.db.begin_stage(j['id'],'GeometryGenerated','fp',{},{});http=FakeHTTP()
        response=Response({});response.ok=False;response.status_code=500
        http.post=lambda *a,**k:response
        with self.assertRaises(ProductionError) as error:ComfyClient(session=http).execute(self.db,a,{}, {})
        self.assertTrue(error.exception.uncertain);self.assertEqual(self.db.attempts(j['id'])[0]['state'],'SubmissionUnknown')
    def test_oom_recorded_quality_reduction_once(self):
        class OOMWorker(FakeWorker):
            def clean_idle_gpu(self,settings):self.cleaned=True
            def execute_stage(self,stage,attempt,asset,settings):
                if stage=='GeometryGenerated' and settings['shape_resolution']>256:
                    self.calls.append(stage);raise ProductionError('CUDA_OOM','injected OOM')
                return super().execute_stage(stage,attempt,asset,settings)
        j=self.job(shape_resolution=384,allow_quality_reduction=True);w=OOMWorker(self.db)
        self.assertEqual(w.run(j['id'])['status'],'Completed');self.assertTrue(w.cleaned)
        self.assertEqual(w.calls.count('GeometryGenerated'),2);self.assertEqual(w.calls.count('ReferenceGenerated'),1)
        self.assertEqual(json.loads(self.db.job(j['id'])['settings_json'])['shape_resolution'],256)
        self.assertEqual(self.db.db.execute("SELECT COUNT(*) FROM JobEvents WHERE event='QualityReducedForOOM'").fetchone()[0],1)
    def test_transient_retry_bounded(self):
        class TransientWorker(FakeWorker):
            def execute_stage(self,stage,*args):
                if stage=='PBRGenerated':self.calls.append(stage);raise ProductionError('ComfyUI_HTTP','injected transient',transient=True)
                return super().execute_stage(stage,*args)
        j=self.job();w=TransientWorker(self.db)
        self.assertEqual(w.run(j['id'])['status'],'Failed');self.assertEqual(w.calls.count('PBRGenerated'),2)
        self.assertEqual(w.calls.count('GeometryGenerated'),1)
    def test_classified_inference_errors(self):
        self.assertEqual(classify('CUDA out of memory','GeometryGenerated'),'CUDA_OOM')
        self.assertEqual(classify('FileNotFound checkpoint','GeometryGenerated'),'ModelLoading')
        self.assertEqual(classify('bad texture','PBRGenerated'),'PBRInference')
    def test_subprocess_nonzero_and_timeout(self):
        for code,timeout in [('import sys;sys.exit(23)',20),('import time;time.sleep(10)',.1)]:
            with self.assertRaises(ProductionError):run_process([sys.executable,'-c',code],self.root,timeout,label='blender')
        self.assertTrue((self.root/'blender.stdout.log').exists());self.assertTrue((self.root/'blender.stderr.log').exists())
    def test_bad_mesh_and_validation_fail(self):
        bad=self.root/'bad.glb';bad.write_text('not a glb')
        with self.assertRaises(Exception):optimize(bad,self.root/'opt',100)
        report=validate(bad,settings_for(self.asset),{},self.root/'absent.json')
        self.assertEqual(report['status'],'FAIL');self.assertTrue(any(c['status']=='WARN' for c in report['checks']))
    def test_real_cpu_cleanup_and_scale(self):
        mesh=trimesh.creation.icosphere(subdivisions=2);mesh.faces=np.vstack([mesh.faces,[0,0,0]])
        path=self.root/'source.glb';mesh.export(path)
        optimized=optimize(path,self.root/'opt',400)
        m=trimesh.load(optimized,force='mesh');self.assertTrue(m.nondegenerate_faces().all());self.assertLessEqual(len(m.faces),400)
        scaled=scale(optimized,self.root/'scale',[4,4],12);m=trimesh.load(scaled,force='mesh')
        self.assertLessEqual(m.extents[0],4.0001);self.assertAlmostEqual(m.extents[1],12,places=4);self.assertAlmostEqual(m.bounds[0,1],0,places=4)
    def test_batch_cli_synthetic_enqueue_only_dedup(self):
        args=['--db',str(self.db.path),'--output-root',str(self.root/'outputs'),'batch','--all']
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(args),0);self.assertEqual(main(args),0)
        jobs=self.db.jobs();self.assertEqual(len(jobs),len(self.catalog));self.assertTrue(all(j['status']=='Pending' for j in jobs));self.assertFalse(any(self.db.attempts(j['id']) for j in jobs))
    def test_cli_controls_and_completed_lookup(self):
        j=self.job();FakeWorker(self.db).run(j['id']);prefix=['--db',str(self.db.path),'--output-root',str(self.root/'outputs')]
        with contextlib.redirect_stdout(io.StringIO()),patch('ai3d_manager.cli.Worker',side_effect=AssertionError('duplicate must not run worker')):
            self.assertEqual(main(prefix+['generate','--asset','DemoCabin']),0)
        for args in (['catalog','show','DemoCabin'],['jobs','list'],['jobs','status',j['id']],['jobs','history',j['id']],
                     ['queue','pause'],['queue','resume'],['jobs','resume',j['id']],['jobs','cancel',j['id']],['jobs','retry',j['id']]):
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(prefix+args),0)
        self.db.set_setting('queue_paused',True)
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(prefix+['queue','run','--limit','1']),0)
    def test_batch_filters(self):
        for flag,value,count in [('--period','demo',8),('--category','Buildings',5)]:
            path=self.root/(value+'.sqlite')
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(['--db',str(path),'--output-root',str(self.root/value),'batch',flag,value]),0)
            db=Database(path);self.assertEqual(len(db.jobs()),count);db.close()
    def test_queue_run_limit_one_fake_worker(self):
        self.job();self.job('002')
        with contextlib.redirect_stdout(io.StringIO()),patch('ai3d_manager.cli.Worker',side_effect=lambda db,**kwargs:FakeWorker(db)):
            self.assertEqual(main(['--db',str(self.db.path),'queue','run','--limit','1']),0)
        self.assertEqual(sorted(j['status'] for j in self.db.jobs()),['Completed','Pending'])
    def test_manual_reference_and_empty_mask(self):
        j=self.job();a=self.db.begin_stage(j['id'],'ReferenceApproved','fp',{},{});p=self.root/'reference.png'
        Image.new('RGBA',(256,256),(0,0,0,0)).save(p);w=Worker(self.db);w.results={'ReferenceProcessed':{'preprocessed':file_info(p)}}
        with self.assertRaises(ProductionError):w.execute_stage('ReferenceApproved',a,self.asset,settings_for(self.asset))
        Image.new('RGBA',(256,256),(0,0,0,255)).save(p);settings=settings_for(self.asset,{'reference_policy':'manual'})
        with self.assertRaises(ProductionError):w.execute_stage('ReferenceApproved',a,self.asset,settings)
        settings['reference_approved']=True
        self.assertTrue(w.execute_stage('ReferenceApproved',a,self.asset,settings)['approval'].exists())
    @unittest.skipUnless((AI/'Backups/phase_1_2/preserved-files.json').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_old_outputs_preserved(self):
        baseline=json.loads((AI/'Backups/phase_1_2/preserved-files.json').read_text('utf-8'))
        records=baseline if isinstance(baseline,list) else baseline.get('files',baseline)
        if isinstance(records,dict):records=[{'path':path,**info} for path,info in records.items()]
        for info in records:self.assertTrue(intact(info),info['path'])
    @unittest.skipUnless((AI/'ComfyUI/custom_nodes/ComfyUI-Hunyuan3d-2-1/hy3dshape/hy3dshape/postprocessors.py').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_vendor_cleanup_preserves_uv_material(self):
        path=AI/'ComfyUI/custom_nodes/ComfyUI-Hunyuan3d-2-1/hy3dshape/hy3dshape/postprocessors.py'
        cls=next(n for n in ast.parse(path.read_text('utf-8')).body if isinstance(n,ast.ClassDef) and n.name=='DegenerateFaceRemover')
        namespace={'synchronize_timer':lambda name:lambda fn:fn}
        exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),namespace)
        mesh=trimesh.creation.box();mesh.faces=np.vstack([mesh.faces,[0,0,0]])
        uv=np.column_stack([np.linspace(0,1,len(mesh.vertices))]*2)
        mesh.visual=trimesh.visual.TextureVisuals(uv=uv,material=trimesh.visual.material.PBRMaterial(name='preserved'))
        clean=namespace['DegenerateFaceRemover']()(mesh)
        self.assertEqual(len(clean.faces),12);self.assertEqual(clean.visual.material.name,'preserved');np.testing.assert_array_equal(clean.visual.uv,uv)
    @unittest.skipUnless((AI/'ComfyUI/custom_nodes/ComfyUI-Hunyuan3d-2-1/hy3dpaint/convert_utils.py').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_pbr_converter_isolated_temporaries(self):
        from concurrent.futures import ThreadPoolExecutor
        path=AI/'ComfyUI/custom_nodes/ComfyUI-Hunyuan3d-2-1/hy3dpaint/convert_utils.py'
        spec=importlib.util.spec_from_file_location('test_converter',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        mesh=trimesh.creation.box();mesh.visual=trimesh.visual.TextureVisuals(uv=np.zeros((len(mesh.vertices),2)))
        obj=self.root/'box.obj';mesh.export(obj)
        for name in ('albedo','metallic','roughness'):Image.new('RGB',(32,32),'gray').save(self.root/(name+'.png'))
        def convert(n):
            out=self.root/f'model{n}.glb'
            module.create_glb_with_pbr_materials(str(obj),{k:str(self.root/(k+'.png')) for k in ('albedo','metallic','roughness')},str(out))
            loaded=trimesh.load(out,force='mesh');self.assertEqual(len(loaded.faces),12)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(convert,range(2)))
        self.assertFalse(list(self.root.glob('g3dc_pbr_*')));self.assertFalse((self.root/'temp.glb').exists())


if __name__=='__main__':
    (AI/'Tests').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
