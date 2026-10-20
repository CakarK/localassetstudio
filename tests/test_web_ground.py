"""Ground-free prompting is retained; web visual approval belongs to the user."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from ai3d_manager import web_control as web
from ai3d_manager.catalog import load_catalog
from ai3d_manager.common import ProductionError,utcnow
from ai3d_manager.worker import Worker,settings_for
from ai3d_manager.web_policy import ground_free_asset,reference_prompt,reference_targets


class GroundReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Game3DGround_')
        root=Path(self.temp.name)
        connector=web.connect
        self.factory=lambda:connector(root/'jobs.sqlite',root/'outputs')
        self.db=self.factory()
        self.original=next(a for a in load_catalog() if a['asset_id']=='DemoCabin')
        self.asset=ground_free_asset(self.original)
    def tearDown(self):
        self.db.close()
        target=Path(self.temp.name).resolve()
        assert target.is_relative_to(Path(tempfile.gettempdir()).resolve()) and target.name.startswith('Game3DGround_')
        for retry in range(6):
            try:self.temp.cleanup();break
            except OSError as exc:
                if retry==5 or getattr(exc,'winerror',None) not in (5,32,145):raise
                time.sleep(.1*(retry+1))
    def job(self):
        self.db.sync_assets([self.asset])
        j,_=self.db.enqueue(self.asset,'ground-test',settings_for(self.asset))
        self.db.db.execute('INSERT INTO WebControls(job_id,updated_at) VALUES (?,?)',(j['id'],utcnow()));self.db.db.commit()
        web.authorize(self.db,j['id'],0)
        return j['id']
    def test_positive_text_preserves_architecture_without_negative_ground_cues(self):
        text='A complete shelter, no windows, with two side doors, no ground mesh, no terrain or soil, no base plate or display pedestal.'
        prepared={'original':'Kaynak korunur','translated':text,'negative':'terrain tile, base plate'}
        result=reference_prompt(prepared,self.asset,{})
        self.assertEqual(result['original'],prepared['original']);self.assertEqual(result['translated'],text)
        self.assertEqual(result['negative'],prepared['negative'])
        self.assertIn('no windows',result['optimized']);self.assertIn('two side doors',result['optimized'])
        self.assertIn('bottom outline follows the walls exactly',result['optimized'])
        for word in ('terrain','soil','base plate','pedestal','city-builder','diorama'):
            self.assertNotIn(word,result['optimized'])
    def test_tree_and_prop_outlines_do_not_invent_buildings(self):
        for change,expected in [({'category':'Nature','phase35_gate_profile':'tree'},'lowest roots'),({'category':'Props','phase35_gate_profile':'prop'},'actual object exactly')]:
            result=reference_prompt({'translated':'The requested complete object.'},{**self.asset,**change},{})
            self.assertIn(expected,result['optimized']);self.assertNotIn('walls',result['optimized'])
    def test_legacy_prompt_fingerprint_is_preserved(self):
        worker=web.InteractiveWorker(self.db,'PromptPrepared');settings=settings_for(self.original)
        self.assertEqual(worker.stage_parameters('PromptPrepared',self.original,settings),Worker(self.db).stage_parameters('PromptPrepared',self.original,settings))
        self.assertIn('web_ground_policy',worker.stage_parameters('PromptPrepared',self.asset,settings))
    def test_historical_ground_policy_remains_frozen_for_existing_prompt_hashes(self):
        stages=reference_targets(self.asset)
        self.assertEqual(stages,['PromptPrepared','ReferenceGenerated','ReferenceFidelity','ReferenceProcessed','ReferenceApproved'])
        self.assertNotIn('GeometryGenerated',stages)
        self.assertEqual(reference_targets(self.original),['PromptPrepared','ReferenceGenerated'])
    def test_preview_is_not_blocked_by_automated_visual_evidence(self):
        ident=self.job();called=[]
        def run(db,job,stage):
            called.append(stage)
            if stage=='ReferenceFidelity':raise ProductionError('GroundFreeReview','Visible base plate')
        with patch.object(web,'connect',self.factory),patch.object(web,'run_pipeline_step',side_effect=run):
            web.run_step(ident,'ReferencePreview')
        row=web.control(self.db,ident)
        self.assertEqual(row['state'],'Waiting');self.assertEqual(row['cursor'],1)
        self.assertEqual(called,['PromptPrepared','ReferenceGenerated'])
    def test_success_only_exposes_reference_for_human_selection(self):
        ident=self.job()
        with patch.object(web,'connect',self.factory),patch.object(web,'run_pipeline_step') as run:
            web.run_step(ident,'ReferencePreview')
        self.assertEqual(web.control(self.db,ident)['state'],'Waiting');self.assertEqual(web.control(self.db,ident)['cursor'],1)
        self.assertEqual([c.args[2] for c in run.call_args_list],['PromptPrepared','ReferenceGenerated'])
        self.assertEqual(self.db.db.execute('SELECT count(*) FROM WebProductionQueue').fetchone()[0],0)


if __name__=='__main__':unittest.main()
