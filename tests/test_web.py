"""Web authorization and orchestration contracts; no GPU inference in these tests."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from ai3d_manager import web_control as control
from ai3d_manager import web_app
from ai3d_manager.catalog import load_catalog
from ai3d_manager.worker import settings_for
from ai3d_manager.common import Halt,ProductionError,utcnow


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        connector=control.connect
        self.factory=lambda:connector(self.root/'web.sqlite',self.root/'outputs')
        self.asset=load_catalog()[0]
        self.db=self.factory();self.db.sync_assets([self.asset])
        self.job,_=self.db.enqueue(self.asset,'test',settings_for(self.asset))
        self.db.db.execute('INSERT INTO WebControls(job_id,updated_at) VALUES (?,?)',(self.job['id'],utcnow()));self.db.db.commit()
        self.app=web_app.create_app({'secret':'test-only-secret','password_hash':generate_password_hash('test-access')})
        self.client=self.app.test_client()
        self.patch=patch.object(web_app,'connect',self.factory);self.patch.start()
        self.qpatch=patch.object(web_app,'QUALITY_PATH',self.root/'absent-quality.sqlite');self.qpatch.start()
    def tearDown(self):
        self.qpatch.stop();self.patch.stop();self.db.close()
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        for retry in range(6):
            try:self.temp.cleanup();break
            except OSError as exc:
                # Windows may briefly retain directory entries after SQLite
                # closes. Retry cleanup only; no test assertion is retried.
                if retry==5 or getattr(exc,'winerror',None) not in (5,32,145):raise
                time.sleep(.1*(retry+1))
    def login(self):
        result=self.client.post('/api/login',json={'code':'test-access'})
        self.assertEqual(result.status_code,200)
        return {'X-CSRF-Token':result.json['csrf']}
    def approve(self,cursor=0):
        self.db.db.execute('UPDATE WebControls SET cursor=? WHERE job_id=?',(cursor,self.job['id']));self.db.db.commit()
        return control.authorize(self.db,self.job['id'],0)

    def test_api_and_artifacts_require_login(self):
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertEqual(self.client.get('/files/library/Depot/model.glb').status_code,401)
    def test_no_csrf_no_mutation(self):
        self.login()
        self.assertEqual(self.client.post('/api/jobs',json={}).status_code,403)
    def test_cross_origin_rejected_even_with_token(self):
        headers=self.login();headers['Origin']='http://foreign.example'
        self.assertEqual(self.client.post('/api/jobs',json={},headers=headers).status_code,403)
    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.client.get('/',headers={'Host':'foreign.example'}).status_code,400)
    def test_login_rate_limit(self):
        for _ in range(5):self.assertEqual(self.client.post('/api/login',json={'code':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'code':'test-access'}).status_code,429)
    def test_stale_tab_cannot_approve_twice(self):
        self.assertEqual(self.approve(),'ReferencePreview')
        with self.assertRaises(ValueError):control.authorize(self.db,self.job['id'],0)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebApprovals').fetchone()[0],1)
    def test_only_one_active_job(self):
        self.approve()
        second,_=self.db.enqueue(self.asset,'second',settings_for(self.asset))
        self.db.db.execute('INSERT INTO WebControls(job_id,updated_at) VALUES (?,?)',(second['id'],utcnow()));self.db.db.commit()
        with self.assertRaises(ValueError):control.authorize(self.db,second['id'],0)
    def test_reference_run_stops_before_mask_or_shape(self):
        self.approve()
        with patch.object(control,'connect',self.factory),patch.object(control,'run_pipeline_step') as run:
            control.run_step(self.job['id'],'ReferencePreview')
        self.assertEqual([c.args[2] for c in run.call_args_list],['PromptPrepared','ReferenceGenerated'])
        self.assertEqual(control.control(self.db,self.job['id'])['cursor'],1)
        self.assertEqual(control.control(self.db,self.job['id'])['state'],'Waiting')
    def test_production_cannot_start_without_reference_choice(self):
        self.approve()
        with patch.object(control,'connect',self.factory),patch.object(control,'run_pipeline_step') as run:
            with self.assertRaises(ValueError):control.run_step(self.job['id'],'Production')
        run.assert_not_called()
    def test_selected_reference_runs_existing_chain(self):
        self.assertEqual(self.approve(1),'Production')
        with patch.object(control,'connect',self.factory),patch.object(control,'run_pipeline_step') as run:
            control.run_step(self.job['id'],'Production')
        calls=[c.args[2] for c in run.call_args_list]
        self.assertEqual(calls,control.PRODUCTION_STEPS);self.assertNotIn('ReferenceFidelity',calls)
        self.assertLess(calls.index('ReferenceProcessed'),calls.index('GeometryGenerated'))
        self.assertIn('Validated',calls);self.assertIn('FinalVisual',calls)
        self.assertEqual(control.control(self.db,self.job['id'])['cursor'],2)
    def test_technical_reference_failure_still_halts_before_shape(self):
        self.approve(1);calls=[]
        def step(db,ident,target):
            calls.append(target)
            if target=='ReferenceApproved':raise ValueError('Empty/invalid foreground')
        with patch.object(control,'connect',self.factory),patch.object(control,'run_pipeline_step',side_effect=step):
            with self.assertRaises(ValueError):control.run_step(self.job['id'],'Production')
        self.assertNotIn('GeometryGenerated',calls);self.assertEqual(control.control(self.db,self.job['id'])['state'],'Blocked')
    def test_human_acceptance_has_no_inference(self):
        self.assertEqual(self.approve(2),'UserAccepted')
        self.assertEqual(control.control(self.db,self.job['id'])['state'],'UserAccepted')
    def test_boundary_prevents_unapproved_next_stage(self):
        worker=control.InteractiveWorker(self.db,'ReferenceGenerated')
        with self.assertRaises(Halt):worker.before_stage('ReferenceProcessed',self.job['id'])
    def test_changed_prior_artifact_cannot_regenerate(self):
        worker=control.InteractiveWorker(self.db,'GeometryGenerated')
        with self.assertRaises(ProductionError):worker.execute_stage('ReferenceGenerated',{},self.asset,{})
    def test_create_does_not_run_gpu_and_duplicate_is_idempotent(self):
        headers=self.login();body={'asset_id':self.asset['asset_id'],'seed':123,'provider':'z-image-turbo'}
        with patch.object(web_app.subprocess,'Popen') as start:
            a=self.client.post('/api/jobs',json=body,headers=headers)
            b=self.client.post('/api/jobs',json=body,headers=headers)
        self.assertEqual(a.status_code,200);self.assertEqual(a.json['job']['id'],b.json['job']['id']);start.assert_not_called()
        self.assertEqual(a.json['job']['control']['cursor'],0)
    def test_arbitrary_provider_or_seed_rejected(self):
        headers=self.login()
        for body in [{'provider':'remote-custom'},{'seed':True}]:
            r=self.client.post('/api/jobs',json={'asset_id':self.asset['asset_id'],**body},headers=headers)
            self.assertEqual(r.status_code,409)
    def test_custom_design_replaces_old_roof_in_quality_source(self):
        from ai3d_manager.reference_fidelity import extract
        headers=self.login()
        edited='A complete garden shed, 3 x 3 meters footprint: wooden panels, a red tile roof, a wide wooden entrance door.'
        payload={'asset_id':'DemoCabin','customization':'Kiremit çatı ve ahşap kapı.'}
        with patch('ai3d_manager.web_design.compose',return_value={'prompt':edited,'original_catalog_prompt':next(a['prompt'] for a in load_catalog() if a['asset_id']=='DemoCabin'),'edits':payload['customization']}):
            composed=self.client.post('/api/design/compose',json=payload,headers=headers)
        self.assertEqual(composed.status_code,200)
        result=self.client.post('/api/jobs',json={**payload,'design_token':composed.json['design_token']},headers=headers)
        self.assertEqual(result.status_code,200)
        request=json.loads(self.db.job(result.json['job']['id'])['request_json'])
        self.assertEqual(request['prompt'],edited)
        criteria=extract(request)['requirements']
        self.assertTrue(any('red tile roof' in r['source']['quote'] for r in criteria))
        self.assertFalse(any('pitched metal roof' in r['source']['quote'] or 'blue painted door' in r['source']['quote'] for r in criteria))
    def test_uncomposed_or_tampered_design_rejected(self):
        headers=self.login()
        for token in ('','forged'):
            result=self.client.post('/api/jobs',json={'asset_id':'DemoCabin','customization':'New roof','design_token':token},headers=headers)
            self.assertEqual(result.status_code,409)
    def test_design_keeps_both_edits_without_old_components(self):
        from ai3d_manager.web_design import compose
        asset=next(a for a in load_catalog() if a['asset_id']=='DemoCabin')
        with patch('ai3d_manager.web_design.guard.translate_prompt',return_value='A red tile roof. A wide wooden door.'):
            result=compose(asset,'Çatı kiremit. Kapı geniş ahşap.')
        self.assertIn('red tile roof',result['prompt']);self.assertIn('wide wooden door',result['prompt'])
        self.assertNotIn('pitched metal roof',result['prompt']);self.assertNotIn('blue painted door',result['prompt'])
        self.assertIn('3 x 3 meters',result['prompt']);self.assertIn('Wooden panels',result['prompt'])
        with patch('ai3d_manager.web_design.guard.translate_prompt',return_value='Add a short chimney on the right side of the roof.'):
            detail=compose(asset,'Çatının sağında kısa bir baca olsun.')
        self.assertIn('pitched metal roof',detail['prompt']);self.assertIn('blue painted door',detail['prompt'])
    def test_recovery_is_explicit_and_does_not_dispatch(self):
        headers=self.login()
        self.db.db.execute("UPDATE WebControls SET state='Blocked',error='Transient' WHERE job_id=?",(self.job['id'],));self.db.db.commit()
        with patch.object(web_app.subprocess,'Popen') as start:
            result=self.client.post('/api/jobs/'+self.job['id']+'/recover',json={'revision':0},headers=headers)
        self.assertEqual(result.status_code,200);start.assert_not_called()
        self.assertEqual(control.control(self.db,self.job['id'])['state'],'Waiting')
    def test_old_jobs_are_not_controllable(self):
        headers=self.login()
        r=self.client.post('/api/jobs/00000000000000000000000000000000/advance',json={'revision':0},headers=headers)
        self.assertEqual(r.status_code,409)
    def test_artifact_path_is_not_browser_supplied(self):
        self.login()
        self.assertEqual(self.client.get('/files/job/'+self.job['id']+'/PromptPrepared/unknown').status_code,404)
        self.assertEqual(self.client.get('/files/library/Depot/..%2F..%2Fweb-config.json').status_code,404)
    def test_real_prompt_preparation_has_provenance(self):
        worker=control.InteractiveWorker(self.db,'PromptPrepared')
        result=worker.run(self.job['id'])
        self.assertEqual(result['status'],'Paused')
        stages=self.db.stages(self.job['id']);self.assertEqual([s['name'] for s in stages],['PromptPrepared'])
        info=self.db.artifact_dict(stages[0]['id'])['prompt']
        self.assertTrue(Path(info['path']).is_file());self.assertEqual(json.loads(Path(info['path']).read_text('utf-8'))['original'],self.asset['prompt'])

if __name__=='__main__':unittest.main()
