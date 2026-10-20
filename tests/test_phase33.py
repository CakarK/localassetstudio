"""Phase3.3 contracts. CPU fixtures/fake VLM are never real generation evidence."""
import copy
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from tests.runtime import TEST_ROOT
from unittest.mock import patch
from PIL import Image,ImageDraw
import trimesh
from ai3d_manager.common import AI,ROOT,ProductionError,atomic_json,canonical,digest,file_info,intact
from ai3d_manager.database import Database
from ai3d_manager.quality_store import QualityStore
from ai3d_manager.worker import Worker,STAGES
from ai3d_manager.catalog import load_catalog
from ai3d_manager.quality_config import load_quality
from ai3d_manager.quality_gates import VERSION,RULES,gate_sources,outputs_for,current_gate
from ai3d_manager.gate_analysis import profile_for,reference_analysis,geometry_analysis
from ai3d_manager.reference_candidates import ReferenceService,candidates,comparison,select_reference,verify_candidate,get_set
from ai3d_manager.controlled_generation import ControlledService,claim_budget,traceability
from ai3d_manager.reference_prompts import compile_prompt

class Phase33Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.asset=next(a for a in load_catalog() if a['asset_id']=='DemoCabin')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name);self.db=Database(self.root/'jobs.sqlite',self.root/'outputs');self.db.sync_assets([self.asset]);self.store=QualityStore(self.root/'quality.sqlite',self.root/'quality')
        self.gate_states={};self.support={};test=self
        class Gates:
            def __init__(self,db,store):self.db=db;self.store=store
            def run(self,kind,jid,outputs=None,**kwargs):
                previous=current_gate(self.db,self.store,jid,kind)
                if previous:return previous
                job=self.db.job(jid);asset=json.loads(job['request_json']);outputs=outputs or outputs_for(self.db,jid);profile=profile_for(asset);sources=gate_sources(kind,outputs)
                slot=asset.get('reference_slot',0);status=test.gate_states.get((kind,slot),test.gate_states.get(kind,'PASS'));ident=uuid.uuid4().hex;work=test.root/'gate-fixtures'/ident;work.mkdir(parents=True)
                cfg=load_quality();atomic_json(work/'gate-vision-request-1.json',{'model':cfg['model'],'options':{k:cfg[k] for k in ('temperature','num_ctx','seed')},'FAKE':True})
                det=reference_analysis(sources['reference']['path'],sources['preprocessed']['path'],profile) if kind=='reference' else geometry_analysis(sources['geometry']['path'],profile)
                criteria={key:{'result':'pass' if status=='PASS' else 'fail' if status=='FAIL' else 'uncertain','confidence':test.support.get(slot,.9),'views':['reference' if kind=='reference' else 'front'],'evidence':'FAKE unit fixture','uncertainty_reason':'FAKE uncertainty' if status=='NeedsReview' else ''} for key in RULES[kind]}
                r={'report_id':ident,'job_id':jid,'kind':kind,'status':status,'version':VERSION,'profile':profile,'source_sha256':digest(asset),'sources':sources,
                   'work_dir':str(work),'model':cfg['model'],'model_digest':cfg['model_digests'][cfg['model']],'created_at':ident,'vlm':{'criteria':criteria},'deterministic':det,'FAKE':True,
                   'report_artifacts':[file_info(work/'gate-vision-request-1.json')]}
                self.store.db.execute('INSERT INTO QualityGateReports VALUES (?,?,?,?,?,?,?,?)',(ident,jid,kind,status,None,ident,str(work),canonical(r)));self.store.db.commit();return r
        class FixtureWorker(Worker):
            def __init__(self,db,**kwargs):super().__init__(db,gate_service_factory=Gates,**kwargs)
            def execute_stage(self,stage,attempt,asset,settings):
                work=Path(attempt['work_dir'])
                if stage in settings.get('reuse_files',{}):return super().execute_stage(stage,attempt,asset,settings)
                if stage in ('ReferenceGenerated','GeometryGenerated','PBRGenerated'):
                    op={'ReferenceGenerated':'reference','GeometryGenerated':'shape','PBRGenerated':'pbr'}[stage]
                    claim_budget(test.store,asset['phase33_set_id'],op,asset.get('reference_slot',0) if op=='reference' else 0,attempt['id'])
                if stage=='ReferenceGenerated':
                    p=work/'reference.png';Image.new('RGB',(256,256),(settings['seed']%255,80,100)).save(p);return {'reference':p}
                if stage=='ReferenceProcessed':
                    p=work/'preprocessed.png';im=Image.open(self.path('ReferenceGenerated','reference')).convert('RGBA');alpha=Image.new('L',im.size);ImageDraw.Draw(alpha).rectangle((45,40,210,215),fill=255);im.putalpha(alpha);im.save(p);return {'preprocessed':p}
                if stage=='GeometryGenerated':
                    p=work/'geometry.glb';trimesh.creation.box(extents=(4,3,5)).export(p);return {'geometry':p}
                keys={'PromptPrepared':['prompt'],'ReferenceApproved':['approval'],'GeometryOptimized':['optimized','optimization_report'],'PBRGenerated':['pbr','albedo','metallic','roughness'],'Scaled':['model','scale_report'],'BlenderExported':['blend','blender_report','fbx','obj','lod1','lod2'],'PreviewsRendered':['preview_front','preview_back'],'Validated':['validation'],'Packaged':['manifest.json']}[stage]
                paths={}
                for k in keys:
                    p=work/(k+'.json');atomic_json(p,{'FAKE':True,'stage':stage,'seed':settings['seed']});paths[k]=p
                return paths
        self.Gates=Gates;self.Worker=FixtureWorker
        self.ref=ReferenceService(self.db,self.store,worker_factory=FixtureWorker,gate_factory=Gates,model_check=lambda:True,monitor_gpu=False)
        self.ctrl=ControlledService(self.db,self.store,worker_factory=FixtureWorker,model_check=lambda:True)
    def tearDown(self):
        self.db.close();self.store.close();assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for n in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if n==7:raise
                time.sleep(.05*(n+1))
    def prepare_selected(self):
        result=self.ref.generate('DemoCabin',3);self.ref.evaluate('DemoCabin');selection=select_reference(self.db,self.store,'DemoCabin');return result,selection
    def fake_visual(self,service,jid,**kwargs):
        e=kwargs['prepared_evaluation'];r={'evaluation_id':e['id'],'job_id':jid,'asset_id':'DemoCabin','decision':'NeedsReview','scores':{'prompt_fidelity':70,'art_style':70,'geometry':80,'texture':70,'top_down_readability':80},'overall_score':73,'FAKE':True}
        service.store.finish(e['id'],r);return r
    def run_controlled(self):
        test=self
        with patch('ai3d_manager.quality_service.QualityService.evaluate',new=lambda service,*a,**kw:test.fake_visual(service,*a,**kw)):return self.ctrl.run('DemoCabin')
    def test_01_multiple_reference_generation(self):
        r=self.ref.generate('DemoCabin',3);self.assertEqual(len(r['candidates']),3);self.assertFalse(r['shape_started']);self.assertEqual({j['status'] for j in self.db.jobs()},{'ReferenceReady'})
    def test_02_unique_seed_tracking(self):
        r=self.ref.generate('DemoCabin',3);self.assertEqual(len({x['seed'] for x in r['candidates']}),3)
    def test_03_reference_candidate_isolation(self):
        r=self.ref.generate('DemoCabin',3);rows=candidates(self.store,r['set_id']);self.assertEqual(len({self.db.job(x['job_id'])['output_dir'] for x in rows}),3)
        self.assertEqual(len({json.loads(x['metadata_json'])['artifacts']['reference']['sha256'] for x in rows}),3)
    def test_04_reference_hash_verification(self):
        r=self.ref.generate('DemoCabin',3);row=r['candidates'][0];p=json.loads(row['metadata_json'])['artifacts']['reference']['path'];Path(p).write_text('tampered')
        with self.assertRaises(ProductionError):self.ref.generate('DemoCabin',3)
        self.assertEqual(len(self.db.jobs()),3)
    def test_05_processed_mask_integrity(self):
        r=self.ref.generate('DemoCabin',3);row=r['candidates'][0];Path(json.loads(row['metadata_json'])['artifacts']['preprocessed']['path']).write_text('tampered')
        with self.assertRaises(ProductionError):verify_candidate(self.db,self.store,row)
    def test_06_reference_gate_states(self):
        self.gate_states={('reference',1):'PASS',('reference',2):'FAIL',('reference',3):'NeedsReview'};self.ref.generate('DemoCabin',3)
        r=self.ref.evaluate('DemoCabin')['comparison'];self.assertEqual(r['pass_count'],1);self.assertEqual([x['gate_status'] for x in r['candidates']],['PASS','FAIL','NeedsReview'])
    def test_07_all_reference_failed_blocks_shape(self):
        self.gate_states={'reference':'FAIL'};self.prepare_selected()
        with self.assertRaises(ProductionError):self.ctrl.run('DemoCabin')
        self.assertFalse(any(s['name']=='GeometryGenerated' for j in self.db.jobs() for s in self.db.stages(j['id'])))
    def test_08_best_reference_not_alpha_max(self):
        self.support={1:.8,2:.95,3:.9};_,selection=self.prepare_selected();rows=candidates(self.store,selection['set_id']);self.assertEqual(selection['selected_reference_id'],rows[1]['id']);self.assertFalse(selection['confidence_is_probability'])
    def test_09_no_shape_before_reference_approval(self):
        self.ref.generate('DemoCabin',3)
        with self.assertRaises(ProductionError):self.ctrl.run('DemoCabin')
        self.assertEqual(len(self.db.jobs()),3);self.assertIsNone(self.db.next_job())
    def test_10_one_shape_limit(self):
        self.prepare_selected();r=self.run_controlled();claims=[dict(x) for x in self.store.db.execute("SELECT * FROM ProductionBudgetClaims WHERE operation='shape'")];self.assertEqual(len(claims),1)
        with self.assertRaises(ProductionError):claim_budget(self.store,r['set_id'],'shape',0,'different-attempt')
    def test_11_geometry_gate_integration(self):
        self.prepare_selected();r=self.run_controlled();self.assertEqual(current_gate(self.db,self.store,r['job_id'],'geometry')['status'],'PASS')
    def test_12_no_pbr_after_geometry_failure(self):
        self.gate_states={'geometry':'FAIL'};self.prepare_selected();r=self.run_controlled();self.assertEqual(r['production_status'],'NeedsReview')
        self.assertFalse(any(x['name']=='PBRGenerated' for x in self.db.stages(r['job_id'])))
    def test_13_stage_resume(self):
        self.gate_states={'geometry':'NeedsReview'};self.prepare_selected();r=self.run_controlled();jid=r['job_id'];before=len([s for s in self.db.stages(jid) if s['name']=='GeometryGenerated'])
        self.gate_states['geometry']='PASS';self.store.db.execute("DELETE FROM QualityGateReports WHERE job_id=? AND kind='geometry'",(jid,));self.store.db.commit();self.db.resume(jid)
        r=self.run_controlled();self.assertEqual(r['production_status'],'Completed');self.assertEqual(len([s for s in self.db.stages(jid) if s['name']=='GeometryGenerated']),before)
    def test_14_production_idempotency(self):
        self.prepare_selected();r=self.run_controlled();before=(len(self.db.jobs()),sum(len(self.db.attempts(j['id'])) for j in self.db.jobs()),self.store.db.execute('SELECT COUNT(*) FROM QualityEvaluations').fetchone()[0])
        state=dict(self.store.db.execute('SELECT * FROM ReferenceSets').fetchone());self.ref.generate('DemoCabin',3)
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM ReferenceSets').fetchone()),state)
        other=self.run_controlled();self.assertEqual(other['job_id'],r['job_id']);self.assertFalse(other['production_started']);self.assertEqual(before,(len(self.db.jobs()),sum(len(self.db.attempts(j['id'])) for j in self.db.jobs()),self.store.db.execute('SELECT COUNT(*) FROM QualityEvaluations').fetchone()[0]))
    def test_15_gpu_reservation(self):
        from ai3d_manager.quality_vlm import check_vlm_reservation
        p=self.root/'reservation.json';atomic_json(p,{'state':'NeedsReview'})
        with self.assertRaises(ProductionError):check_vlm_reservation(p)
    def test_16_output_preservation(self):
        self.ref.generate('DemoCabin',3);old={str(p):file_info(p) for p in (self.root/'outputs').rglob('*') if p.is_file()};self.ref.generate('DemoCabin',3)
        for info in old.values():self.assertTrue(intact(info))
    def test_17_qwen_digest_verification(self):
        from ai3d_manager.quality_vlm import OllamaVLM
        from tests.test_quality import Response
        class HTTP:
            def get(self,url,**kw):return Response({'version':'FAKE'} if url.endswith('/version') else {'models':[{'name':load_quality()['model'],'digest':'WRONG'}]})
        with self.assertRaises(ProductionError):OllamaVLM(session=HTTP()).metadata()
    def test_18_baseline_comparison_scope(self):
        from ai3d_manager.controlled_generation import baseline_for
        from ai3d_manager.worker import settings_for
        self.assertIsNone(baseline_for(self.db,self.store,'DemoCabin'))
        base=self.db.enqueue(self.asset,'historical',settings_for(self.asset))[0];self.db.status(base['id'],'Completed');e=self.store.begin(base,{})
        full={'visual_evaluation_id':e['id']};self.store.db.execute('INSERT INTO QualityGateReports VALUES (?,?,?,?,?,?,?,?)',('FAKE-baseline',base['id'],'full','NeedsReview',None,'2026','FAKE',canonical(full)));self.store.db.commit()
        self.prepare_selected();r=self.run_controlled();self.assertEqual(r['baseline'],{'job_id':base['id'],'evaluation_id':e['id']})
    def test_19_failed_reference_not_selected(self):
        self.gate_states={('reference',1):'FAIL'};_,s=self.prepare_selected();rows=candidates(self.store,s['set_id']);self.assertNotEqual(s['selected_reference_id'],rows[0]['id'])
    def test_20_sqlite_persistence(self):
        r=self.ref.generate('DemoCabin',3);s=QualityStore(self.store.path,self.store.output_root);self.assertEqual(get_set(s,'DemoCabin')['set_id'],r['set_id']);self.assertEqual(len(candidates(s,r['set_id'])),3);s.close()
    def test_21_restart_recovery_budget_owner(self):
        plan=self.ref.prepare('DemoCabin');claim_budget(self.store,plan['set_id'],'shape',0,'owner');claim_budget(self.store,plan['set_id'],'shape',0,'owner')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ProductionBudgetClaims').fetchone()[0],1)
        with self.assertRaises(ProductionError):claim_budget(self.store,plan['set_id'],'shape',0,'new-owner')
    def test_22_reference_to_mesh_provenance(self):
        self.prepare_selected();r=self.run_controlled();trace=json.loads(Path(r['traceability']).read_text('utf-8'));self.assertEqual(len(trace['checkpoints']),len(STAGES));self.assertEqual(trace['reference_candidate_id'],r['reference_candidate_id'])
        self.assertTrue(all(i['sha256'] for c in trace['checkpoints'] for i in c['outputs'].values()));self.assertTrue(compile_prompt(self.asset)['no_hunyuan_text_conditioning'])
    def test_23_budget_enforcement(self):
        with self.assertRaises(ValueError):self.ref.prepare('DemoCabin',4)
        plan=self.ref.prepare('DemoCabin',3)
        with self.assertRaises(ProductionError):self.ref.prepare('DemoCabin',2)
        self.assertEqual(plan['budget'],{'references':3,'shape':1,'pbr':1,'final_evaluations':1})
    @unittest.skipUnless((AI/'Backups/phase_3_3/source/ai3d_manager/validation.py').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_24_technical_validation_preserved(self):
        p=ROOT/'ai3d_manager/validation.py';saved=AI/'Backups/phase_3_3/source/ai3d_manager/validation.py';self.assertEqual(file_info(p)['sha256'],file_info(saved)['sha256'])
        prompt=compile_prompt(self.asset);self.assertEqual(prompt['shared_style'],self.asset['shared_style']);self.assertEqual(prompt['original_prompt'],self.asset['prompt'])

if __name__=='__main__':unittest.main(verbosity=2)
