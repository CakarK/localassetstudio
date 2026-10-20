"""3D selection/scheduling contracts. PNG fixtures are not generated asset evidence."""
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image
from werkzeug.security import generate_password_hash
from ai3d_manager import web_app,web_queue,web_control
from ai3d_manager.catalog import load_catalog
from ai3d_manager.common import file_info,utcnow
from ai3d_manager.worker import settings_for
from ai3d_manager.web_assets import asset_from_request

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Game3DWebQueue_');self.root=Path(self.temp.name)
        connector=web_control.connect
        self.factory=lambda:connector(self.root/'queue.sqlite',self.root/'outputs')
        self.db=self.factory();self.asset=next(a for a in load_catalog() if a['asset_id']=='DemoCabin');self.db.sync_assets([self.asset])
    def tearDown(self):
        self.db.close()
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()) and self.root.name.startswith('Game3DWebQueue_')
        for retry in range(6):
            try:self.temp.cleanup();break
            except OSError:
                if retry==5:raise
                time.sleep(.1*(retry+1))
    def ready(self,name):
        job,_=self.db.enqueue(self.asset,name,settings_for(self.asset));ident=job['id']
        attempt=self.db.begin_stage(ident,'ReferenceGenerated','fixture-only',{},{});path=Path(attempt['work_dir'])/'reference.png'
        Image.new('RGB',(128,128),'gray').save(path);self.db.complete_stage(attempt,{'reference':file_info(path)})
        self.db.db.execute("INSERT INTO WebControls(job_id,cursor,updated_at) VALUES (?,1,?)",(ident,utcnow()));self.db.db.commit()
        return {'id':ident,'revision':0}
    def start(self):self.db.set_setting('web_queue_running',True)
    def app(self):
        self.patch=patch.object(web_app,'connect',self.factory);self.patch.start();self.addCleanup(self.patch.stop)
        self.qpatch=patch.object(web_app,'QUALITY_PATH',self.root/'no-quality.sqlite');self.qpatch.start();self.addCleanup(self.qpatch.stop)
        app=web_app.create_app({'secret':'test-queue-secret','password_hash':generate_password_hash('test')})
        client=app.test_client();login=client.post('/api/login',json={'code':'test'})
        return app,client,{'X-CSRF-Token':login.json['csrf']}

    def test_selected_images_only_and_no_preview_queue(self):
        a,b,c=[self.ready(x) for x in ['a','b','unselected']]
        web_queue.add(self.db,[a,b]);self.start()
        self.assertEqual(web_queue.claim_next(self.db),a['id'])
        self.assertEqual(web_control.control(self.db,c['id'])['state'],'Waiting')
        approvals=self.db.db.execute('SELECT job_id,target FROM WebApprovals').fetchall()
        self.assertEqual({r['target'] for r in approvals},{'Production'});self.assertNotIn(c['id'],{r['job_id'] for r in approvals})
    def test_add_requires_explicit_start(self):
        a=self.ready('a');web_queue.add(self.db,[a])
        self.assertIsNone(web_queue.claim_next(self.db));self.assertEqual(web_control.control(self.db,a['id'])['state'],'Queued')
    def test_duplicate_selection_is_idempotent(self):
        a=self.ready('a');first=web_queue.add(self.db,[a]);self.assertEqual(first,web_queue.add(self.db,[a]))
        self.assertEqual(len(web_queue.rows(self.db)),1);self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebApprovals').fetchone()[0],1)
    def test_missing_reference_rolls_back_whole_selection(self):
        a=self.ready('a');b=self.ready('b');Path(web_control.snapshot(self.db,b['id'])['ReferenceGenerated']['reference']['path']).unlink()
        with self.assertRaises(ValueError):web_queue.add(self.db,[a,b])
        self.assertEqual(web_queue.rows(self.db),[]);self.assertEqual(web_control.control(self.db,a['id'])['state'],'Waiting')
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebApprovals').fetchone()[0],0)
    def test_reorder_controls_next_model(self):
        a,b=[self.ready(x) for x in ['a','b']];ids=web_queue.add(self.db,[a,b]);web_queue.reorder(self.db,ids[::-1]);self.start()
        self.assertEqual(web_queue.claim_next(self.db),b['id'])
    def test_stale_reorder_rejected(self):
        ids=web_queue.add(self.db,[self.ready('a'),self.ready('b')])
        with self.assertRaises(ValueError):web_queue.reorder(self.db,ids[:1])
    def test_one_active_model_and_pause_at_model_boundary(self):
        a,b=[self.ready(x) for x in ['a','b']];web_queue.add(self.db,[a,b]);self.start()
        self.assertEqual(web_queue.claim_next(self.db),a['id']);self.assertIsNone(web_queue.claim_next(self.db))
        self.db.set_setting('web_queue_running',False)
        self.db.db.execute("UPDATE WebControls SET state='Waiting',cursor=2 WHERE job_id=?",(a['id'],));self.db.db.commit();web_queue.settle(self.db)
        self.assertIsNone(web_queue.claim_next(self.db));self.assertEqual(web_control.control(self.db,b['id'])['state'],'Queued')
        self.assertEqual(web_queue.rows(self.db)[0]['state'],'Done')
    def test_blocked_model_does_not_stop_independent_next(self):
        a,b=[self.ready(x) for x in ['a','b']];web_queue.add(self.db,[a,b]);self.start();web_queue.claim_next(self.db)
        self.db.db.execute("UPDATE WebControls SET state='Blocked',error='Reference gate FAIL' WHERE job_id=?",(a['id'],));self.db.db.commit();web_queue.settle(self.db)
        self.assertEqual(web_queue.claim_next(self.db),b['id']);self.assertEqual(web_queue.rows(self.db)[0]['state'],'Blocked')
    def test_tampered_reference_never_dispatched(self):
        a,b=[self.ready(x) for x in ['a','b']];web_queue.add(self.db,[a,b]);self.start()
        Path(web_control.snapshot(self.db,a['id'])['ReferenceGenerated']['reference']['path']).write_bytes(b'changed fixture')
        self.assertEqual(web_queue.claim_next(self.db),b['id']);self.assertEqual(web_queue.rows(self.db)[0]['state'],'Blocked')
    def test_remove_retains_reference_for_later_selection(self):
        a=self.ready('a');ids=web_queue.add(self.db,[a]);before=web_control.snapshot(self.db,a['id']);web_queue.remove(self.db,ids[0])
        self.assertEqual(web_control.control(self.db,a['id'])['state'],'Waiting');self.assertEqual(before,web_control.snapshot(self.db,a['id']))
        with self.assertRaises(ValueError):web_queue.remove(self.db,ids[0])
    def test_stale_selection_rejected(self):
        a=self.ready('a');a['revision']=5
        with self.assertRaises(ValueError):web_queue.add(self.db,[a])
    def test_queue_order_and_authorizations_survive_reopen(self):
        a,b=[self.ready(x) for x in ['a','b']];ids=web_queue.add(self.db,[a,b]);web_queue.reorder(self.db,ids[::-1]);self.start()
        self.db.close();self.db=self.factory();self.assertEqual(web_queue.claim_next(self.db),b['id'])
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebApprovals').fetchone()[0],2)
    def test_crashed_item_is_not_auto_retried_and_empty_queue_stops(self):
        a=self.ready('a');web_queue.add(self.db,[a]);self.start();web_queue.claim_next(self.db)
        self.db.db.execute("UPDATE WebControls SET state='Blocked' WHERE job_id=?",(a['id'],));self.db.db.commit();web_queue.settle(self.db)
        self.assertIsNone(web_queue.claim_next(self.db));self.assertFalse(self.db.setting('web_queue_running'))
        self.assertEqual(web_control.control(self.db,a['id'])['state'],'Blocked')
    def test_concurrent_claims_cannot_start_two_models(self):
        web_queue.add(self.db,[self.ready('a'),self.ready('b')]);self.start();barrier=threading.Barrier(2)
        def claim():
            db=self.factory()
            try:barrier.wait(timeout=5);return web_queue.claim_next(db)
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(lambda _:claim(),range(2)))
        self.assertEqual(sum(r is not None for r in result),1)
    def test_scheduler_launches_only_selected_production(self):
        a,b,c=[self.ready(x) for x in ['a','b','unselected']];app,client,headers=self.app()
        fake=Mock();fake.pid=os.getpid();fake.poll.return_value=None
        with patch.object(web_app.subprocess,'Popen',return_value=fake) as launch:
            response=client.post('/api/queue/add',json={'selections':[a,b]},headers=headers);self.assertEqual(response.status_code,200);launch.assert_not_called()
            self.assertEqual(client.post('/api/queue/start',json={},headers=headers).status_code,200)
            app.extensions['studio_queue_tick']();app.extensions['studio_queue_tick']();self.assertEqual(launch.call_count,1)
            self.assertEqual(launch.call_args.args[0][-2:],[a['id'],'Production'])
            self.db.db.execute("UPDATE WebControls SET state='Waiting',cursor=2 WHERE job_id=?",(a['id'],));self.db.db.commit()
            app.extensions['studio_queue_tick']();self.assertEqual(launch.call_count,2)
            self.assertEqual(launch.call_args.args[0][-2:],[b['id'],'Production'])
        self.assertEqual(web_control.control(self.db,c['id'])['state'],'Waiting')
    def test_queue_api_requires_csrf(self):
        _,client,headers=self.app()
        for endpoint in ('start','pause','add','reorder'):
            self.assertEqual(client.post('/api/queue/'+endpoint,json={}).status_code,403)
    def test_from_scratch_asset_has_real_user_source_and_existing_profile(self):
        _,client,headers=self.app()
        payload={'source_mode':'custom','name':'Hurda jeneratör','prompt':'A complete rusty portable generator with a steel frame and two small wheels.',
                 'prompt_language':'en','gate_profile':'prop','footprint_m':[2,1],'polygon_target':2500,'seed':765}
        with patch.object(web_app.subprocess,'Popen') as start:
            result=client.post('/api/jobs',json=payload,headers=headers)
        self.assertEqual(result.status_code,200);start.assert_not_called()
        job=self.db.job(result.json['job']['id']);request=json.loads(job['request_json']);settings=json.loads(job['settings_json'])
        self.assertTrue(job['asset_id'].startswith('Custom_'));self.assertEqual(request['prompt'],payload['prompt'])
        self.assertEqual(request['source']['kind'],'web_custom_prompt');self.assertEqual(request['phase35_gate_profile'],'prop')
        self.assertEqual(settings['polygon_target'],2500);self.assertEqual(settings['prompt_language'],'en');self.assertNotIn('Neutral product rendering',request['shared_style']);self.assertEqual(request['origins']['style'],'user')
        again=client.post('/api/jobs',json=payload,headers=headers);self.assertEqual(again.json['job']['id'],job['id'])
    def test_invalid_custom_asset_parameters_rejected(self):
        base={'source_mode':'custom','name':'New prop','prompt':'A complete simple wooden salvage crate.'}
        for change in [{'gate_profile':'new-unverified'},{'polygon_target':True},{'footprint_m':[0,4]},{'height_m':float('nan')},{'prompt':''}]:
            with self.assertRaises(ValueError):asset_from_request({**base,**change})
    def test_new_jobs_require_ground_free_images_and_geometry(self):
        from ai3d_manager.quality_requirements import requirements
        _,client,headers=self.app()
        result=client.post('/api/jobs',json={'asset_id':'DemoCabin','seed':9345},headers=headers)
        self.assertEqual(result.status_code,200)
        job=self.db.job(result.json['job']['id']);asset=json.loads(job['request_json']);settings=json.loads(job['settings_json'])
        self.assertTrue(asset['ground_free_required']);self.assertIn('No ground mesh',asset['shared_style']);self.assertIn('terrain tile',settings['negative'])
        self.assertNotIn('flat base on the ground',asset['shared_style'])
        criterion=next(r for r in requirements(asset)['requirements'] if r['id']=='GE01')
        self.assertEqual(criterion['importance'],'critical');self.assertIn('soil patch',criterion['description'])
    def test_ground_evidence_blocks_ground_or_uncertain_outputs(self):
        from ai3d_manager.web_policy import require_ground_free
        from ai3d_manager.common import ProductionError
        for requirement in [None,{'id':'GE01','result':'Unmet','confidence':.95}, {'id':'GE01','result':'Uncertain','confidence':.8}, {'id':'GE01','result':'Met','confidence':.4},{'id':'GE01','result':'not_met','confidence':.95}]:
            with self.assertRaises(ProductionError):require_ground_free({'requirements':[requirement] if requirement else []})
        require_ground_free({'requirements':[{'id':'GE01','result':'Met','confidence':.9}]})
        require_ground_free({'requirements':[{'id':'GE01','result':'met','confidence':.9}]})

    def test_human_can_choose_old_ai_blocked_reference_without_regeneration(self):
        a=self.ready('old-ai-fail');before=web_control.snapshot(self.db,a['id'])
        self.db.db.execute("UPDATE WebControls SET cursor=0,state='Blocked',error='Prompt uyumu FAIL; Shape engellendi.' WHERE job_id=?",(a['id'],));self.db.db.commit()
        app,client,headers=self.app()
        job=next(j for j in client.get('/api/state').json['jobs'] if j['id']==a['id'])
        self.assertTrue(job['reference_ready'])
        with patch.object(web_app.subprocess,'Popen') as launch:
            result=client.post('/api/queue/add',json={'selections':[a]},headers=headers)
        self.assertEqual(result.status_code,200);launch.assert_not_called()
        row=web_control.control(self.db,a['id']);self.assertEqual((row['state'],row['cursor']),('Queued',1))
        self.assertIsNone(row['error']);self.assertEqual(before,web_control.snapshot(self.db,a['id']))
        self.start();self.assertEqual(web_queue.claim_next(self.db),a['id'])
        proof=web_control.human_reference_approval(self.db,a['id'])
        self.assertEqual(proof['reference'],before['ReferenceGenerated']['reference'])
    def test_human_approval_skips_only_reference_vlm_gates(self):
        from ai3d_manager.worker import Worker
        a=self.ready('human-choice');web_queue.add(self.db,[a]);self.start();web_queue.claim_next(self.db)
        worker=web_control.InteractiveWorker(self.db,'PBRGenerated')
        with patch.object(Worker,'before_stage') as parent,patch('ai3d_manager.reference_fidelity.current_fidelity',side_effect=AssertionError('Reference VLM forbidden')):
            worker.before_stage('ReferenceApproved',a['id']);worker.before_stage('GeometryGenerated',a['id']);parent.assert_not_called()
            worker.before_stage('GeometryOptimized',a['id']);parent.assert_called_once_with('GeometryOptimized',a['id'])
            worker.before_stage('PBRGenerated',a['id']);self.assertEqual(parent.call_count,2)
    def test_human_reference_approval_requires_actual_bound_snapshot(self):
        from ai3d_manager.common import ProductionError
        a=self.ready('no-human-proof');worker=web_control.InteractiveWorker(self.db,'GeometryGenerated')
        with self.assertRaises(ProductionError):worker.before_stage('GeometryGenerated',a['id'])
        web_queue.add(self.db,[a]);self.start();web_queue.claim_next(self.db)
        Path(web_control.snapshot(self.db,a['id'])['ReferenceGenerated']['reference']['path']).write_bytes(b'altered after approval')
        with self.assertRaises(ProductionError):worker.before_stage('GeometryGenerated',a['id'])
    def test_technical_mask_check_remains_after_human_approval(self):
        from ai3d_manager.common import ProductionError
        a=self.ready('empty-mask');web_queue.add(self.db,[a]);self.start();web_queue.claim_next(self.db)
        mask=self.root/'empty-mask.png';Image.new('RGBA',(128,128),(0,0,0,0)).save(mask)
        worker=web_control.InteractiveWorker(self.db,'ReferenceApproved')
        worker.results={'ReferenceProcessed':{'preprocessed':file_info(mask)}}
        attempt=self.db.begin_stage(a['id'],'ReferenceApproved','test-only',{},{});
        with self.assertRaises(ProductionError):worker.execute_stage('ReferenceApproved',attempt,self.asset,settings_for(self.asset))
    def test_reference_preparation_records_real_human_approval_without_fake_vlm_pass(self):
        a=self.ready('valid-mask');web_queue.add(self.db,[a]);self.start();web_queue.claim_next(self.db)
        mask=self.root/'valid-mask.png';Image.new('RGBA',(128,128),(100,100,100,255)).save(mask)
        worker=web_control.InteractiveWorker(self.db,'ReferenceApproved');worker.results={'ReferenceProcessed':{'preprocessed':file_info(mask)}}
        attempt=self.db.begin_stage(a['id'],'ReferenceApproved','test-only',{},{});
        result=worker.execute_stage('ReferenceApproved',attempt,self.asset,settings_for(self.asset));record=json.loads(result['approval'].read_text('utf-8'))
        self.assertEqual(record['semantic_visual_approval'],'human')
        self.assertEqual(record['web_human_approval']['reference'],web_control.snapshot(self.db,a['id'])['ReferenceGenerated']['reference'])
        self.assertNotIn('vlm',record);self.assertNotIn('PASS',record.values())

    def test_explicit_prop_geometry_profile_is_shared_by_cli_and_web(self):
        from ai3d_manager.gate_analysis import profile_for
        bus=next(a for a in load_catalog() if a['asset_id']=='DemoFigure')
        self.assertEqual(profile_for(bus)['name'],'prop')
        profile=web_control.web_gate_profile(bus)
        self.assertEqual(profile['name'],'prop')
        self.assertEqual(profile['geometry'],profile_for(bus,'prop')['geometry'])
        self.assertEqual(web_control.web_gate_profile({**bus,'phase35_gate_profile':'fence'})['name'],'fence')
        self.assertEqual(web_control.web_gate_profile({'asset_id':'UnmappedObject','category':'Unknown'})['name'],'unknown')
    def test_only_web_gate_factory_injects_web_profile_resolution(self):
        from ai3d_manager.quality_gates import QualityGateService
        from ai3d_manager.gate_analysis import profile_for
        self.assertIs(QualityGateService(self.db,None).profile_resolver,profile_for)
        self.assertIs(web_control.gate_factory(self.db,None).profile_resolver,web_control.web_gate_profile)

if __name__=='__main__':unittest.main()
