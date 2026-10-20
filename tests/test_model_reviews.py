"""Human review actions and model identity; isolated fixtures, no production inference."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import trimesh
from werkzeug.security import generate_password_hash
from ai3d_manager import web_app,web_control
from ai3d_manager.common import file_info,utcnow
from ai3d_manager.catalog import load_catalog
from ai3d_manager.worker import settings_for
from tests.runtime import TEST_ROOT


class ModelReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name)
        connect=web_control.connect;self.factory=lambda:connect(self.root/'web.sqlite',self.root/'outputs')
        self.db=self.factory();self.asset=load_catalog()[0];self.db.sync_assets([self.asset])
        self.job=self.db.enqueue(self.asset,'review-fixture',settings_for(self.asset))[0]
        self.db.db.execute('INSERT INTO WebControls(job_id,updated_at) VALUES (?,?)',(self.job['id'],utcnow()));self.db.db.commit()
        self.app=web_app.create_app({'secret':'isolated-review-test','password_hash':generate_password_hash('test-access')});self.client=self.app.test_client()
        self.patches=[patch.object(web_app,'connect',self.factory),patch.object(web_app,'QUALITY_PATH',self.root/'missing-quality.sqlite'),patch.object(web_app,'AI',self.root/'AI3D')]
        for p in self.patches:p.start()
        self.subject='web:'+self.job['id'];self.url='/api/model-reviews/'+self.subject
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.close();assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for retry in range(6):
            try:self.temp.cleanup();break
            except OSError as exc:
                if retry==5 or getattr(exc,'winerror',None) not in (5,32,145):raise
                time.sleep(.1*(retry+1))
    def login(self):
        r=self.client.post('/api/login',json={'code':'test-access'});self.assertEqual(r.status_code,200)
        self.headers={'X-CSRF-Token':r.json['csrf']};return self.headers
    def model(self,final=True,technical='WARN'):
        stage='Packaged' if final else 'GeometryGenerated';name='model.glb' if final else 'geometry'
        a=self.db.begin_stage(self.job['id'],stage,'isolated-fixture',{},{});path=Path(a['work_dir'])/'model.glb'
        trimesh.creation.box().export(path);self.db.complete_stage(a,{name:file_info(path)})
        if final:
            a=self.db.begin_stage(self.job['id'],'Validated','isolated-fixture',{},{});p=Path(a['work_dir'])/'validation.json'
            p.write_text(json.dumps({'status':technical,'checks':[],'FAKE_test_report':True}),'utf-8');self.db.complete_stage(a,{'validation':file_info(p)})
        self.db.db.execute('UPDATE WebControls SET cursor=? WHERE job_id=?',(2 if final else 1,self.job['id']));self.db.db.commit()
        return path
    def detail(self):
        r=self.client.get(self.url);self.assertEqual(r.status_code,200);return r.json['model']
    def body(self,action='Accepted',note=''):
        m=self.detail();return {'action':action,'note':note,'revision':m['revision'],'review_version':m['review_version'],'model_sha256':m['model']['sha256']}
    def test_review_and_model_routes_require_session_csrf_and_same_origin(self):
        self.model();self.assertEqual(self.client.get('/reviews').status_code,200)
        for path in ('/api/model-reviews',self.url,self.url+'/model'):self.assertEqual(self.client.get(path).status_code,401)
        self.login();body=self.body()
        self.assertEqual(self.client.post(self.url,json=body).status_code,403)
        self.assertEqual(self.client.post(self.url,json=body,headers={**self.headers,'Origin':'http://foreign.example'}).status_code,403)
        self.assertFalse(self.db.db.execute('SELECT 1 FROM WebModelReviews').fetchone())
    def test_raw_model_can_be_human_accepted_without_fabricating_pipeline_completion(self):
        self.login();self.assertEqual(self.client.get('/api/model-reviews').json['models'],[])
        self.model(final=False);data=self.detail();self.assertEqual(data['model']['label'],'Ham geometri');self.assertTrue(data['can_accept'])
        before=self.db.job(self.job['id']);outputs=web_control.snapshot(self.db,self.job['id'])
        with patch.object(web_app.subprocess,'Popen') as start:
            result=self.client.post(self.url,json=self.body(),headers=self.headers)
        self.assertEqual(result.status_code,200);start.assert_not_called();self.assertEqual(result.json['model']['review_status'],'Accepted')
        self.assertEqual(result.json['model']['acceptance_scope'],'model_only');self.assertIsNone(result.json['model']['technical'])
        row=web_control.control(self.db,self.job['id']);self.assertEqual((row['state'],row['cursor']),('Waiting',1))
        self.assertEqual(self.db.job(self.job['id']),before);self.assertEqual(web_control.snapshot(self.db,self.job['id']),outputs)
        self.assertFalse(self.db.db.execute("SELECT 1 FROM WebApprovals WHERE target='UserAccepted'").fetchone())
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM WebApprovals WHERE target='ModelAccepted'").fetchone()[0],1)
        self.assertEqual(self.client.get('/api/model-reviews').json['models'][0]['subject'],self.subject)
    def test_glb_is_inline_and_supports_range_without_arbitrary_file_access(self):
        path=self.model();self.login();r=self.client.get(self.url+'/model',headers={'Range':'bytes=0-11'})
        try:self.assertEqual(r.status_code,206);self.assertEqual(r.mimetype,'model/gltf-binary');self.assertEqual(r.data,path.read_bytes()[:12]);self.assertNotIn('attachment',r.headers.get('Content-Disposition',''))
        finally:r.close()
        self.assertEqual(self.client.get('/api/model-reviews/library:unknown/model').status_code,409)
    def test_accept_uses_existing_final_approval_and_never_dispatches_or_changes_job(self):
        path=self.model();self.login();before=self.db.job(self.job['id']);info=file_info(path);body=self.body()
        with patch.object(web_app.subprocess,'Popen') as start:
            result=self.client.post(self.url,json=body,headers=self.headers)
        self.assertEqual(result.status_code,200);start.assert_not_called();self.assertEqual(file_info(path),info);self.assertEqual(before,self.db.job(self.job['id']))
        row=web_control.control(self.db,self.job['id']);self.assertEqual((row['state'],row['cursor']),('UserAccepted',3))
        self.assertEqual(result.json['model']['review_status'],'Accepted')
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM WebApprovals WHERE target='UserAccepted'").fetchone()[0],1)
        self.assertEqual(self.client.post(self.url,json=body,headers=self.headers).status_code,409)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebModelReviews').fetchone()[0],1)
    def test_revision_and_rejection_are_persistent_notes_without_cancel_or_regeneration(self):
        path=self.model();self.login();before=self.db.job(self.job['id']);info=file_info(path)
        for action,note in [('RevisionRequested','Çatıyı değiştir; zemin plakasını kaldır.'),('Rejected','İstenen yapının bütünlüğü yok.')]:
            with patch.object(web_app.subprocess,'Popen') as start:
                r=self.client.post(self.url,json=self.body(action,note),headers=self.headers)
            self.assertEqual(r.status_code,200);start.assert_not_called();self.assertEqual(r.json['model']['review_status'],action);self.assertEqual(r.json['model']['history'][0]['note'],note)
        self.assertEqual(before,self.db.job(self.job['id']));self.assertEqual(info,file_info(path));self.assertEqual(len(self.detail()['history']),2)
        other=self.factory()
        try:self.assertEqual(other.db.execute('SELECT count(*) FROM WebModelReviews').fetchone()[0],2)
        finally:other.close()
    def test_revision_revokes_acceptance_without_erasing_approval_history(self):
        self.model();self.login();self.client.post(self.url,json=self.body(),headers=self.headers)
        r=self.client.post(self.url,json=self.body('RevisionRequested','Kapı çerçevesini düzelt.'),headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual([x['action'] for x in r.json['model']['history']],['RevisionRequested','Accepted'])
        row=web_control.control(self.db,self.job['id']);self.assertEqual((row['state'],row['cursor']),('Waiting',2))
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebApprovals').fetchone()[0],1)
    def test_tampered_model_or_technical_failure_cannot_be_accepted(self):
        path=self.model();self.login();body=self.body();path.write_bytes(path.read_bytes()+b'changed')
        self.assertEqual(self.client.post(self.url,json=body,headers=self.headers).status_code,409)
        self.assertEqual(self.client.get(self.url+'/model').status_code,409)
        self.assertFalse(self.db.db.execute('SELECT 1 FROM WebModelReviews').fetchone())
    def test_blocked_raw_and_failed_technical_reports_do_not_overrule_human_acceptance(self):
        self.model(final=False);self.login()
        self.db.db.execute("UPDATE WebControls SET state='Blocked',error='GeometryIdentityGateFailed' WHERE job_id=?",(self.job['id'],));self.db.db.commit()
        stages=web_control.snapshot(self.db,self.job['id']);before=self.db.job(self.job['id'])
        r=self.client.post(self.url,json=self.body('Accepted','Model tam istediğim gibi.'),headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['model']['review_status'],'Accepted')
        row=web_control.control(self.db,self.job['id']);self.assertEqual((row['state'],row['cursor'],row['error']),('Blocked',1,'GeometryIdentityGateFailed'))
        self.assertEqual(self.db.job(self.job['id']),before);self.assertEqual(stages,web_control.snapshot(self.db,self.job['id']))
        self.model(final=True,technical='FAIL')
        self.db.db.execute("UPDATE WebControls SET state='Blocked' WHERE job_id=?",(self.job['id'],));self.db.db.commit()
        r=self.client.post(self.url,json=self.body('Accepted'),headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['model']['technical']['status'],'FAIL')
        self.assertEqual(web_control.control(self.db,self.job['id'])['state'],'Blocked')
    def test_active_or_failed_jobs_keep_their_production_controls(self):
        self.model(final=False);self.login()
        for state in ('Running','Queued'):
            self.db.db.execute('UPDATE WebControls SET state=? WHERE job_id=?',(state,self.job['id']));self.db.db.commit()
            self.assertEqual(self.client.post(self.url,json=self.body('RevisionRequested','Düzeltme gerekli.'),headers=self.headers).status_code,409)
        self.db.db.execute("UPDATE WebControls SET state='Blocked',error='GeometryIdentityGateFailed' WHERE job_id=?",(self.job['id'],));self.db.db.commit()
        r=self.client.post(self.url,json=self.body('RevisionRequested','Bütün yapı eksik.'),headers=self.headers)
        self.assertEqual(r.status_code,200);row=web_control.control(self.db,self.job['id']);self.assertEqual(row['state'],'Blocked');self.assertEqual(row['error'],'GeometryIdentityGateFailed')
    def test_invalid_empty_notes_and_stale_review_versions_rejected(self):
        self.model();self.login();body=self.body('RevisionRequested','Giriş değişsin.')
        for values in ({'note':' '},{'note':'x'*4001},{'revision':True},{'review_version':True},{'action':'made-up'}):
            self.assertEqual(self.client.post(self.url,json={**body,**values},headers=self.headers).status_code,409)
        self.client.post(self.url,json=body,headers=self.headers)
        fresh=self.body('Rejected','Yeni ret nedeni.')
        self.assertEqual(self.client.post(self.url,json={**fresh,'review_version':0},headers=self.headers).status_code,409)
    def test_old_studio_acceptance_is_visible_in_new_history(self):
        self.model();self.login();self.assertEqual(web_control.authorize(self.db,self.job['id'],0),'UserAccepted')
        result=self.detail();self.assertEqual(result['review_status'],'Accepted');self.assertEqual(result['history'][0]['action'],'Accepted')
    def test_legacy_library_review_preserves_manifest_and_old_job_databases(self):
        directory=self.root/'AI3D';manifest=directory/'Library/manifest.json';manifest.parent.mkdir(parents=True)
        asset={'name_tr':'İzole sprint modeli','score':80,'triangles':12,'status':'ReadyForReview','technical_status':'WARN','provider':'fixture','reason':''}
        manifest.write_text(json.dumps({'assets':{'LegacyOne':asset}}),'utf-8');before=manifest.read_bytes()
        output=directory/'Outputs/Library/LegacyOne';output.mkdir(parents=True)
        trimesh.creation.box().export(output/'model.glb');(output/'validation.json').write_text('{"status":"WARN","checks":[]}',encoding='utf-8')
        self.login();self.url='/api/model-reviews/library:LegacyOne'
        body=self.body('Accepted','İzole test kararı.');r=self.client.post(self.url,json=body,headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['model']['review_status'],'Accepted');self.assertEqual(manifest.read_bytes(),before)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM GenerationJobs').fetchone()[0],1)


if __name__=='__main__':unittest.main()
