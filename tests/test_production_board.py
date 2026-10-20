"""Visible queue and dispatcher share the same order; no inference."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image
from tests import test_model_reviews as fixtures
from ai3d_manager import web_production,web_control,web_queue,web_revisions,web_app
from ai3d_manager.common import file_info,utcnow
from ai3d_manager.prompts import guard


class ProductionBoardTests(unittest.TestCase):
    setUp=fixtures.ModelReviewTests.setUp
    tearDown=fixtures.ModelReviewTests.tearDown
    model=fixtures.ModelReviewTests.model
    login=fixtures.ModelReviewTests.login
    body=fixtures.ModelReviewTests.body
    detail=fixtures.ModelReviewTests.detail

    def review(self,note='Kapıya ahşap malzeme ekle.'):
        self.model();self.login()
        r=self.client.post(self.url,json=self.body('RevisionRequested',note),headers=self.headers)
        self.assertEqual(r.status_code,200);return r.json['review_id']

    def manual(self):
        job=self.db.enqueue(self.asset,'manual-board-fixture',json.loads(self.job['settings_json']))[0]
        self.db.db.execute('INSERT INTO WebControls(job_id,cursor,updated_at) VALUES (?,1,?)',(job['id'],utcnow()));self.db.db.commit()
        a=self.db.begin_stage(job['id'],'ReferenceGenerated','fixture',{},{});p=Path(a['work_dir'])/'reference.png';Image.new('RGB',(64,64),'white').save(p)
        self.db.complete_stage(a,{'reference':file_info(p)});web_queue.add(self.db,[{'id':job['id'],'revision':0}]);return job

    def board(self):return web_production.board(self.db,self.root/'absent-quality.sqlite')

    def test_pending_review_has_name_note_and_position_before_child_exists_and_no_duplicate(self):
        rid=self.review();b=self.client.get('/api/production').json['production']
        self.assertEqual(len(b['waiting']),1);self.assertEqual(b['waiting'][0]['name'],self.asset['name_tr'])
        self.assertEqual(b['waiting'][0]['note'],'Kapıya ahşap malzeme ekle.');self.assertIsNone(b['waiting'][0]['job_id'])
        self.assertEqual(b['waiting'][0]['key'],'r:'+str(rid));self.assertEqual(b['active'],[]);self.assertTrue(b['running']);self.assertFalse(b['paused'])
        with patch.object(guard,'translate_prompt',return_value='Use a wooden door.'):
            child=web_revisions.prepare(self.db,web_revisions.pending(self.db)[0],self.root/'AI3D')
        self.db.db.execute("UPDATE WebControls SET state='Waiting',cursor=1 WHERE job_id=?",(child,));self.db.db.commit()
        a=self.db.begin_stage(child,'ReferenceGenerated','fixture',{},{});p=Path(a['work_dir'])/'ref.png';Image.new('RGB',(64,64),'white').save(p);self.db.complete_stage(a,{'reference':file_info(p)})
        web_revisions.advance(self.db,web_revisions.pending(self.db)[0],Mock())
        self.assertEqual(len(self.board()['waiting']),1)

    def test_named_running_stage_and_final_visual_are_real_and_stage_failures_are_not_renamed(self):
        self.model();self.db.db.execute("UPDATE WebControls SET state='Running',cursor=1 WHERE job_id=?",(self.job['id'],));self.db.db.commit()
        a=self.db.begin_stage(self.job['id'],'BlenderExported','fixture',{},{});b=self.board();active=b['active'][0]
        self.assertEqual(active['name'],self.asset['name_tr']);self.assertEqual(active['stage'],'BlenderExported');self.assertEqual(active['stage_started_at'],a['started_at'])
        self.db.db.execute("UPDATE JobStages SET status='Completed' WHERE id=?",(a['stage_id'],));self.db.db.commit()
        self.assertEqual(self.board()['active'][0]['stage'],'FinalVisual')

    def test_visible_reorder_drives_dispatch_across_manual_and_review_work_and_survives_reopen(self):
        rid=self.review();manual=self.manual();self.db.set_setting('web_queue_running',True)
        before=self.board()['waiting'];self.assertEqual(before[0]['kind'],'manual')
        keys=[i['key'] for i in before][::-1];r=self.client.post('/api/production/reorder',json={'keys':keys},headers=self.headers);self.assertEqual(r.status_code,200)
        reopened=self.factory()
        try:self.assertEqual([i['key'] for i in web_production.candidates(reopened)],keys)
        finally:reopened.close()
        proc=Mock(pid=999999);proc.poll.return_value=None
        with patch.object(guard,'translate_prompt',return_value='Use weathered wood.'),patch.object(web_app.subprocess,'Popen',return_value=proc) as launch:
            self.app.extensions['studio_queue_tick']()
        child=self.db.db.execute('SELECT child_job_id FROM WebReviewProductions WHERE review_id=?',(rid,)).fetchone()[0]
        self.assertEqual(launch.call_args.args[0][-2:],[child,'ReferencePreview']);self.assertEqual(web_control.control(self.db,manual['id'])['state'],'Queued')
        self.assertEqual(self.board()['active'][0]['key'],keys[0])

    def test_stale_or_duplicate_order_and_running_removal_are_rejected(self):
        rid=self.review();self.manual();self.db.set_setting('web_queue_running',True);keys=[i['key'] for i in self.board()['waiting']]
        for invalid in (keys[:1],[keys[0],keys[0]],['unknown']):
            self.assertEqual(self.client.post('/api/production/reorder',json={'keys':invalid},headers=self.headers).status_code,409)
        self.db.db.execute("UPDATE WebControls SET state='Running' WHERE job_id=?",(self.job['id'],));self.db.db.commit()
        # This is the source model, not a scheduled child: it is never removed.
        self.assertEqual(self.client.post('/api/production/remove',json={'key':'job:'+self.job['id']},headers=self.headers).status_code,409)

    def test_cancel_pending_or_preparing_does_not_erase_parent_or_dispatch_after_cpu_returns(self):
        rid=self.review();before=web_control.snapshot(self.db,self.job['id']);key='r:'+str(rid)
        from ai3d_manager.web_design import compose as real_compose
        def compose_and_cancel(asset,note,**kwargs):
            web_production.cancel(self.db,key);return real_compose(asset,note,**kwargs)
        with patch('ai3d_manager.web_design.compose',side_effect=compose_and_cancel),patch.object(guard,'translate_prompt',return_value='Use wood materials.'),patch.object(web_app.subprocess,'Popen') as launch:
            tick=self.app.extensions['studio_queue_tick'];tick();tick();launch.assert_not_called()
        task=self.db.db.execute('SELECT state,child_job_id FROM WebReviewProductions WHERE review_id=?',(rid,)).fetchone()
        self.assertEqual(task['state'],'Cancelled');self.assertEqual(web_control.control(self.db,task['child_job_id'])['state'],'Rejected')
        self.assertEqual(before,web_control.snapshot(self.db,self.job['id']));self.assertFalse(self.board()['waiting'])

    def test_paused_manual_work_does_not_hide_automatic_revision_and_resume_start_is_consistent(self):
        rid=self.review();self.manual();b=self.board()
        self.assertEqual(b['waiting'][0]['key'],'r:'+str(rid));self.assertFalse(b['waiting'][1]['enabled'])
        self.client.post('/api/queue/pause',json={},headers=self.headers);self.assertTrue(self.board()['paused'])
        self.assertFalse(any(i['enabled'] for i in self.board()['waiting']))
        self.client.post('/api/queue/start',json={},headers=self.headers);self.assertTrue(all(i['enabled'] for i in self.board()['waiting']))

    def test_recovered_model_history_uses_current_result_and_pending_cancel_survives_enroll(self):
        rid=self.review();manual=self.manual();self.db.db.execute("UPDATE WebProductionQueue SET state='Blocked',error='old failure' WHERE job_id=?",(manual['id'],))
        self.db.db.execute("UPDATE WebControls SET cursor=2,state='Waiting',error=NULL WHERE job_id=?",(manual['id'],));self.db.db.commit()
        row=next(i for i in self.board()['history'] if i['job_id']==manual['id']);self.assertEqual(row['state'],'Done')
        self.assertIsNone(row['error']);self.assertEqual(self.board()['counts']['ready'],1)
        self.db.db.execute('INSERT INTO WebModelReviews(subject,action,note,model_json,created_at) VALUES (?,?,?,?,?)',('web:'+manual['id'],'RevisionRequested','Yeni kaplama gerekli.','{}',utcnow()));self.db.db.commit()
        self.assertEqual(self.board()['counts']['ready'],0);self.assertEqual(next(i for i in self.board()['history'] if i['job_id']==manual['id'])['review_state'],'RevisionRequested')
        self.client.post('/api/production/remove',json={'key':'r:'+str(rid)},headers=self.headers)
        web_revisions.enroll(self.db);self.assertEqual([t['subject'] for t in web_revisions.pending(self.db)],['web:'+manual['id']])

    def test_board_routes_require_session_and_csrf_and_do_not_dispatch_on_read(self):
        self.assertEqual(self.client.get('/api/production').status_code,401);self.login()
        self.assertEqual(self.client.post('/api/production/reorder',json={'keys':[]}).status_code,403)
        with patch.object(web_app.subprocess,'Popen') as launch:
            self.assertEqual(self.client.get('/api/production').status_code,200);launch.assert_not_called()

