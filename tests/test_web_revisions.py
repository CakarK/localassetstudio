"""Review-driven regeneration orchestration; no real inference or production data."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from tests import test_model_reviews as fixtures
from ai3d_manager import web_control,web_revisions,web_queue,web_app
from ai3d_manager.common import file_info,utcnow
from ai3d_manager.prompts import guard


class RevisionProductionTests(unittest.TestCase):
    setUp=fixtures.ModelReviewTests.setUp
    tearDown=fixtures.ModelReviewTests.tearDown
    model=fixtures.ModelReviewTests.model
    login=fixtures.ModelReviewTests.login
    body=fixtures.ModelReviewTests.body
    detail=fixtures.ModelReviewTests.detail

    def request_revision(self,note='Kapıyı değiştir.',action='RevisionRequested'):
        self.model();self.login()
        r=self.client.post(self.url,json=self.body(action,note),headers=self.headers)
        self.assertEqual(r.status_code,200);return r.json['review_id']

    def task(self,review_id):
        return dict(self.db.db.execute('SELECT * FROM WebReviewProductions WHERE review_id=?',(review_id,)).fetchone())

    def prepare(self,review_id):
        with patch.object(guard,'translate_prompt',return_value='Replace the door with a wooden door.'):
            return web_revisions.prepare(self.db,self.task(review_id),self.root/'AI3D')

    def reference(self,ident):
        a=self.db.begin_stage(ident,'ReferenceGenerated','isolated-reference',{},{});p=Path(a['work_dir'])/'reference.png'
        from PIL import Image
        Image.new('RGB',(64,64),'white').save(p);self.db.complete_stage(a,{'reference':file_info(p)})
        self.db.db.execute("UPDATE WebControls SET state='Waiting',cursor=1,pid=NULL WHERE job_id=?",(ident,));self.db.db.commit()

    def test_decision_persists_one_task_atomically_and_stale_post_cannot_duplicate(self):
        self.model();self.login();body=self.body('Rejected','Çatı çökmüş, düzelt.')
        r=self.client.post(self.url,json=body,headers=self.headers);self.assertEqual(r.status_code,200)
        task=r.json['model']['revision_production'];self.assertEqual(task['state'],'Pending');self.assertIsNone(task['child_job_id'])
        self.assertEqual(self.client.post(self.url,json=body,headers=self.headers).status_code,409)
        web_revisions.enroll(self.db);web_revisions.enroll(self.db)
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebReviewProductions').fetchone()[0],1)

    def test_review_composition_resolves_faults_on_cpu_and_normal_edits_keep_old_contract(self):
        from ai3d_manager.web_design import compose
        response=Mock();response.json.return_value={'done':True,'done_reason':'stop','message':{'content':json.dumps({'prompt':'Doors exclusively on the right-hand side; left panels without doors.'})}}
        with patch.object(guard.requests,'post',return_value=response) as post:
            value=compose(self.asset,'İki tarafta kapı var; sadece sağda olsun.',review_feedback=True)
        body=post.call_args.kwargs['json'];self.assertEqual(body['options']['num_gpu'],0);self.assertEqual(body['keep_alive'],0)
        self.assertEqual(body['model'],'qwen3:4b');self.assertIn('never restate an observed defect',body['messages'][0]['content'])
        self.assertTrue(value['review_feedback_resolved']);self.assertIn('Doors exclusively',value['prompt']);self.assertEqual(value['edits'],'İki tarafta kapı var; sadece sağda olsun.')
        with patch.object(guard,'translate_prompt',return_value='A wooden door.') as translate:
            normal=compose(self.asset,'Ahşap kapı olsun.')
        self.assertNotIn('post',translate.call_args.kwargs);self.assertFalse(normal['review_feedback_resolved'])

    def test_notes_apply_to_customized_prompt_and_prior_files_settings_are_unchanged(self):
        review=self.request_revision('Kapıyı ahşap yap.')
        before=self.db.job(self.job['id']);original=web_control.snapshot(self.db,self.job['id'])
        ident=self.prepare(review);child=self.db.job(ident);a=json.loads(child['request_json']);s=json.loads(child['settings_json'])
        self.assertIn('Replace the door with a wooden door.',a['prompt']);self.assertEqual(a['web_revision']['note'],'Kapıyı ahşap yap.')
        self.assertEqual(a['web_revision']['parent_subject'],self.subject);self.assertTrue(a['ground_free_required'])
        self.assertEqual(s['max_transient_retries'],0);self.assertNotEqual(s['seed'],json.loads(before['settings_json'])['seed'])
        self.assertEqual(a['polygon_target'],self.asset['polygon_target']);self.assertEqual(before,self.db.job(self.job['id']))
        self.assertEqual(original,web_control.snapshot(self.db,self.job['id']))
        self.assertNotEqual(ident,self.job['id']);self.assertEqual(len(self.db.jobs()),2)
        self.assertEqual(self.prepare(review),ident);self.assertEqual(len(self.db.jobs()),2)
        # Simulate a crash after enqueue but before the child link is committed.
        web_revisions.update(self.db,review,child_job_id=None,state='Preparing')
        with patch('ai3d_manager.web_design.compose',side_effect=AssertionError('must reuse durable child')):
            self.assertEqual(web_revisions.prepare(self.db,self.task(review),self.root/'AI3D'),ident)

    def test_reference_then_automatic_queue_and_exact_file_authorization(self):
        review=self.request_revision();ident=self.prepare(review);launch=Mock()
        web_revisions.advance(self.db,self.task(review),launch);launch.assert_called_once_with(self.db,ident,'ReferencePreview')
        self.reference(ident);web_revisions.advance(self.db,self.task(review),launch)
        self.assertEqual(self.task(review)['state'],'Queued');self.assertFalse(self.db.setting('web_queue_running',False))
        self.assertEqual(web_queue.claim_next(self.db,[ident]),ident)
        approved=web_control.human_reference_approval(self.db,ident)
        self.assertEqual(approved['policy'],'review_regeneration_delegation');self.assertEqual(approved['review_id'],review)
        self.assertEqual(approved['reference'],web_control.snapshot(self.db,ident)['ReferenceGenerated']['reference'])
        ref=Path(approved['reference']['path']);ref.write_bytes(ref.read_bytes()+b'changed')
        with self.assertRaisesRegex(Exception,'ApprovalInvalidated|Seçilen görsel'):web_control.human_reference_approval(self.db,ident)

    def test_turkish_source_prompt_is_translated_before_english_revision_is_appended(self):
        review=self.request_revision('Yıpranmış ahşap malzeme ekle.')
        settings=json.loads(self.db.job(self.job['id'])['settings_json']);settings.update(prompt='Kütükten yapılmış önü açık barınak.',prompt_language='tr')
        self.db.db.execute('UPDATE GenerationJobs SET settings_json=? WHERE id=?',(json.dumps(settings),self.job['id']));self.db.db.commit()
        with patch.object(guard,'translate_prompt',side_effect=['A log shelter with an open front.','Use weathered wood material.']):
            ident=web_revisions.prepare(self.db,self.task(review),self.root/'AI3D')
        asset=json.loads(self.db.job(ident)['request_json'])
        self.assertIn('A log shelter with an open front.',asset['prompt']);self.assertIn('Use weathered wood material.',asset['prompt'])
        self.assertNotIn('Kütükten',asset['prompt']);self.assertIn('source_prompt_translation',asset['web_composition'])

    def test_scheduler_serializes_all_stages_and_final_candidate_is_not_accepted(self):
        review=self.request_revision()
        process=Mock(pid=999999);process.poll.return_value=None
        with patch.object(guard,'translate_prompt',return_value='Use a wooden door.'),patch.object(web_app.subprocess,'Popen',return_value=process) as start:
            tick=self.app.extensions['studio_queue_tick'];tick();task=self.task(review);ident=task['child_job_id']
            self.assertEqual(start.call_count,1);tick();self.assertEqual(start.call_count,1)
            self.reference(ident);tick();self.assertEqual(self.task(review)['state'],'Queued');tick();self.assertEqual(start.call_count,2)
            tick();self.assertEqual(start.call_count,2)
            self.db.db.execute("UPDATE WebControls SET state='Waiting',cursor=2,pid=NULL WHERE job_id=?",(ident,));self.db.db.commit()
            tick();tick();self.assertEqual(self.task(review)['state'],'Done');self.assertEqual(start.call_count,2)
            self.assertEqual(web_control.control(self.db,ident)['state'],'Waiting')
            self.assertFalse(self.db.db.execute("SELECT 1 FROM WebApprovals WHERE job_id=? AND target='UserAccepted'",(ident,)).fetchone())

    def test_pause_persists_and_manual_references_do_not_start_implicitly(self):
        review=self.request_revision();ident=self.prepare(review);self.reference(ident)
        web_revisions.advance(self.db,self.task(review),Mock())
        # A separate ordinary queue item stays stopped despite automatic work.
        other=self.db.enqueue(self.asset,'ordinary-reference',json.loads(self.job['settings_json']))[0]
        self.db.db.execute('INSERT INTO WebControls(job_id,updated_at) VALUES (?,?)',(other['id'],utcnow()));self.db.db.commit()
        self.reference(other['id']);web_queue.add(self.db,[{'id':other['id'],'revision':0}])
        self.assertEqual(self.client.post('/api/queue/pause',json={},headers=self.headers).status_code,200)
        self.assertIsNone(web_queue.claim_next(self.db,[ident]));other_db=self.factory()
        try:self.assertTrue(other_db.setting('web_revision_paused'))
        finally:other_db.close()
        self.db.set_setting('web_revision_paused',False)
        self.assertEqual(web_queue.claim_next(self.db,[ident]),ident)
        self.db.db.execute("UPDATE WebControls SET state='Waiting',cursor=2 WHERE job_id=?",(ident,));self.db.db.commit()
        self.assertIsNone(web_queue.claim_next(self.db,[ident]));self.assertEqual(web_control.control(self.db,other['id'])['state'],'Queued')

    def test_removed_queue_item_and_failed_shape_are_never_retried(self):
        review=self.request_revision();ident=self.prepare(review);self.reference(ident)
        web_revisions.advance(self.db,self.task(review),Mock());web_queue.remove(self.db,self.task(review)['queue_id'])
        web_revisions.reconcile(self.db);self.assertEqual(self.task(review)['state'],'Cancelled')
        web_revisions.enroll(self.db);self.assertFalse(web_revisions.pending(self.db))
        second=self.client.post(self.url,json=self.body('Rejected','Tam yapı gerekli.'),headers=self.headers).json['review_id']
        child=self.prepare(second);self.db.db.execute("UPDATE WebControls SET state='Blocked',error='GeometryIdentityGateFailed' WHERE job_id=?",(child,));self.db.db.commit()
        web_revisions.reconcile(self.db);web_revisions.enroll(self.db)
        self.assertEqual(self.task(second)['state'],'Blocked');self.assertEqual(len(self.db.jobs()),3)

    def test_superseded_decisions_cancel_only_unstarted_revisions(self):
        review=self.request_revision();ident=self.prepare(review);self.reference(ident);web_revisions.advance(self.db,self.task(review),Mock())
        self.client.post(self.url,json=self.body('Accepted'),headers=self.headers)
        web_revisions.reconcile(self.db);self.assertEqual(self.task(review)['state'],'Superseded')
        self.assertEqual(self.db.db.execute('SELECT state FROM WebProductionQueue WHERE job_id=?',(ident,)).fetchone()[0],'Cancelled')
        self.assertEqual(web_control.control(self.db,self.job['id'])['state'],'UserAccepted')
        web_revisions.enroll(self.db);self.assertFalse(web_revisions.pending(self.db))

    def test_tampered_source_and_translation_error_block_once_then_next_independent_task_runs(self):
        review=self.request_revision()
        with patch('ai3d_manager.web_design.compose',side_effect=ValueError('translation failed')),patch.object(web_app.subprocess,'Popen') as launch:
            tick=self.app.extensions['studio_queue_tick'];tick();tick();launch.assert_not_called()
        self.assertEqual(self.task(review)['state'],'Blocked');self.assertIn('translation failed',self.task(review)['error'])
        self.assertEqual(len(self.db.jobs()),1)
        second=self.client.post(self.url,json=self.body('RevisionRequested','Taş malzeme ekle.'),headers=self.headers).json['review_id']
        model=web_control.snapshot(self.db,self.job['id'])['Packaged']['model.glb'];p=Path(model['path']);p.write_bytes(p.read_bytes()+b'tampered')
        with self.assertRaisesRegex(ValueError,'kaynak model'):self.prepare(second)

    def test_sprint_revision_reads_original_request_without_mutating_legacy_database(self):
        from ai3d_manager.common import canonical
        root=self.root/'AI3D';(root/'State').mkdir(parents=True);(root/'Library').mkdir(parents=True)
        path=root/'State/library_jobs.sqlite';old=web_control.connect(path,root/'old-outputs')
        original={**self.asset,'prompt':'A log workshop with an open entrance.'};old.sync_assets([original]);job=old.enqueue(original,'legacy',json.loads(self.job['settings_json']))[0]
        model=self.model();old.close()
        manifest=root/'Library/manifest.json';manifest.write_text(canonical({'assets':{'Workshop':{'job_id':job['id'],'source_database':str(path)}}}),'utf-8')
        before=path.read_bytes();before_manifest=manifest.read_bytes()
        self.db.db.execute('INSERT INTO WebModelReviews(subject,action,note,model_json,created_at) VALUES (?,?,?,?,?)',
            ('library:Workshop','RevisionRequested','Zemin kalkmalı.',canonical({'info':file_info(model)}),utcnow()));self.db.db.commit()
        web_revisions.enroll(self.db);task=next(t for t in web_revisions.pending(self.db) if t['subject']=='library:Workshop')
        with patch.object(guard,'translate_prompt',return_value='Remove the terrain and base plate.'):
            ident=web_revisions.prepare(self.db,task,root)
        asset=json.loads(self.db.job(ident)['request_json']);self.assertIn('A log workshop',asset['prompt'])
        self.assertEqual(asset['web_revision']['parent_subject'],'library:Workshop');self.assertEqual(before,path.read_bytes());self.assertEqual(before_manifest,manifest.read_bytes())

