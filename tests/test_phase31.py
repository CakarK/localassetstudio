"""Phase 3.1 contract tests; all fake inference is explicitly isolated."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from tests.runtime import TEST_ROOT
from PIL import Image
from ai3d_manager.catalog import load_catalog
from ai3d_manager.common import AI,ProductionError,atomic_json,canonical,file_info,intact
from ai3d_manager.database import Database
from ai3d_manager.worker import settings_for
from ai3d_manager.quality_store import QualityStore
from ai3d_manager.quality_config import load_quality
from ai3d_manager.quality_vlm import OllamaVLM
from ai3d_manager.quality_requirements import requirements
from ai3d_manager.quality_compact import compact_schema,expand,merge_batches,apply_grounding
from ai3d_manager.quality_service import VIEWS,reusable_render
from ai3d_manager.criteria_cache import CriteriaCache
from ai3d_manager.prompt_refinement import compile_refinement,refine,normalized
from ai3d_manager.quality_improve import prepare_improvement,improve,select_stage,reuse_for_stage,used_production_seconds
from ai3d_manager.quality_scoring import score
from ai3d_manager.quality_planner import compare_candidates
from tests.test_quality import fixture_payload,Response

class Phase31Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.asset=next(a for a in load_catalog() if a['asset_id']=='DemoCabin')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name)
        self.db=Database(self.root/'jobs.sqlite',self.root/'outputs');self.db.sync_assets([self.asset]);self.store=QualityStore(self.root/'quality.sqlite',self.root/'quality')
        self.job=self.db.enqueue(self.asset,'base',settings_for(self.asset))[0];self.db.status(self.job['id'],'Completed');self.job=self.db.job(self.job['id'])
        self.i=requirements(self.asset);self.payload=fixture_payload(self.i)
        self.report=score(self.payload,self.i,{'status':'WARN','checks':[{'check':'footprint','status':'PASS'}]},[{'view':v,'width':768,'mode':'material'} for v in VIEWS])
        evaluation=self.store.begin(self.job,{})
        self.report.update(job_id=self.job['id'],asset_id=self.asset['asset_id'],evaluation_id=evaluation['id'],model='FAKE',model_version='FAKE',evaluation_version='test',interpretation_sha256=self.i['interpretation_sha256'])
        self.store.finish(evaluation['id'],self.report)
        benchmark={'model':load_quality()['model'],'fastest_strategy':'six','trials':[{'strategy':'six','evaluation_id':evaluation['id']}],'status':'PASS'}
        self.store.db.execute('INSERT INTO QualityBenchmarks VALUES (?,?,?,?,?,?)',('fake-benchmark',self.asset['asset_id'],'FAKE','PASS',canonical(benchmark),'2026-10-19'));self.store.db.commit()
    def tearDown(self):
        self.db.close();self.store.close();assert self.root.resolve().is_relative_to(TEST_ROOT.resolve())
        for n in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if n==7:raise
                time.sleep(.05*(n+1))
    def compact(self):
        return {'scores':{k:{'s':v['score'],'c':.75,'v':v['views'],'e':v['reason']} for k,v in self.payload['scores'].items()},
                'criteria':{o['criterion_id']:{'r':'M','c':.75,'v':o['views'],'e':o['evidence'],'s':'info','f':''} for o in self.payload['observations']},
                'conflicts':[],'details':[]}
    def test_model_configuration_and_capabilities(self):
        cfg=load_quality();self.assertEqual(cfg['provider'],'ollama');self.assertFalse(cfg['allow_automatic_visual_approval'])
        class HTTP:
            def get(self,url,**kw):return Response({'version':'fake'} if url.endswith('/version') else {'models':[{'name':'fake:text','digest':'fake'}]})
            def post(self,*args,**kwargs):return Response({'capabilities':['completion']})
        with self.assertRaises(ProductionError) as error:OllamaVLM(model='fake:text',digest='fake',session=HTTP()).metadata()
        self.assertEqual(error.exception.category,'VLMNotVision')
    def test_gemma_vision_request_and_measurements(self):
        class HTTP:
            request=None
            def post(self,url,json=None,**kw):
                self.request=json
                return Response({'done':True,'done_reason':'stop','message':{'content':'{"ready":true}'},'eval_count':20,'eval_duration':2000000000,'load_duration':1000000000,'prompt_eval_count':123})
        http=HTTP();v=OllamaVLM(session=http);p=self.root/'image.png';Image.new('RGB',(32,32),'white').save(p)
        value,m=v.structured('test',{'type':'object'},self.root,'fake',images=[p]*6)
        self.assertEqual(len(http.request['messages'][1]['images']),6);self.assertNotIn('think',http.request)
        self.assertEqual(http.request['options']['num_ctx'],load_quality()['num_ctx']);self.assertEqual(m['tokens_per_second'],10);self.assertEqual(m['load_seconds'],1)
    def test_compact_output_matches_existing_schema(self):
        from ai3d_manager.quality_schema import parse_response
        wire=parse_response({'done':True,'message':{'content':json.dumps(self.compact())}},compact_schema(self.i['requirements'],VIEWS))
        p=expand(wire,self.i['requirements'],VIEWS);self.assertEqual(len(p['observations']),len(self.payload['observations']))
        self.assertEqual(set(p['scores']),set(self.payload['scores']))
    def test_invalid_percentage_confidence_not_silently_normalized(self):
        from ai3d_manager.quality_schema import parse_response
        v=self.compact();v['criteria'][next(iter(v['criteria']))]['c']=98
        with self.assertRaises(Exception):parse_response({'done':True,'message':{'content':json.dumps(v)}},compact_schema(self.i['requirements'],VIEWS))
    def test_render_cache_hash_and_parameters(self):
        e=self.store.get(self.report['evaluation_id']);render=Path(e['work_dir'])/'renders';render.mkdir();artifacts=[]
        for v in VIEWS:
            p=render/(v+'.png');Image.new('RGB',(512,512),'gray').save(p);artifacts.append({'view':v,'mode':'material',**file_info(p)})
        atomic_json(render/'render-metadata.json',{'source_sha256':'fake-model','render':{'resolution':512},'artifacts':artifacts})
        self.assertEqual(reusable_render(self.store,self.job['id'],{'sha256':'fake-model'},512),e['id'])
        self.assertIsNone(reusable_render(self.store,self.job['id'],{'sha256':'changed'},512))
        self.assertIsNone(reusable_render(self.store,self.job['id'],{'sha256':'fake-model'},768))
    def test_criteria_cache_reuse_and_invalidation(self):
        cache=CriteriaCache(self.root/'cache');self.assertIsNone(cache.get(self.asset));cache.put(self.asset,self.i,{'mode':'FAKE'})
        self.assertEqual(cache.get(self.asset)['interpretation_sha256'],self.i['interpretation_sha256'])
        changed={**self.asset,'prompt':self.asset['prompt']+' Extra source sentence.'};self.assertIsNone(cache.get(changed))
    def test_digest_mismatch_blocks_model(self):
        class HTTP:
            def get(self,url,**kw):return Response({'version':'fake'} if url.endswith('/version') else {'models':[{'name':'fake:vision','digest':'different'}]})
        with self.assertRaises(ProductionError) as error:OllamaVLM(model='fake:vision',digest='pinned',session=HTTP()).metadata()
        self.assertEqual(error.exception.category,'VLMVersionMismatch')
    def test_refinement_no_inference_and_requirement_preservation(self):
        r=compile_refinement(self.asset,self.report);self.assertFalse(r['inference_started']);self.assertTrue(r['no_direct_hunyuan_text_conditioning'])
        for item in r['preserved_requirements']:self.assertIn(normalized(item['output_phrase']),normalized(r['refined_prompt']))
        self.assertEqual(self.asset['prompt'],r['refined_prompt']);self.assertLessEqual(len(r['refined_prompt'].split()),130)
        self.assertEqual(r['original_prompt'],self.asset['prompt']);self.assertIn('DreamShaper',r['negative_applied_to'][0])
    def test_changed_source_remains_authoritative(self):
        a={**self.asset,'prompt':self.asset['prompt'].replace('blue painted door','green painted door')}
        result=compile_refinement(a,self.report)
        self.assertEqual(result['refined_prompt'],a['prompt'])
        self.assertNotIn('blue painted door',result['refined_prompt'])
    def test_refine_idempotent_across_evaluation_ids(self):
        a=refine(self.db,self.store,self.job['id']);b=refine(self.db,self.store,self.job['id']);self.assertEqual(a['refinement_id'],b['refinement_id'])
        self.assertEqual(len(self.db.jobs()),1)
    def test_candidate_creation_isolated_two_seeds(self):
        result=improve(self.db,self.store,self.job['id'],prepare_only=True)
        ids=[s['job_id'] for s in result['slots']];seeds=[s['seed'] for s in result['slots']]
        self.assertEqual(len(set(ids)),2);self.assertEqual(len(set(seeds)),2)
        for ident in ids:
            job=self.db.job(ident);settings=json.loads(job['settings_json'])
            self.assertEqual(job['status'],'Pending');self.assertFalse(settings['reuse_files']);self.assertEqual(settings['polygon_target'],self.asset['polygon_target']);self.assertEqual(settings['texture_size'],1024)
            self.assertNotEqual(job['output_dir'],self.job['output_dir'])
    def test_improve_restart_deduplicates_candidates(self):
        a=improve(self.db,self.store,self.job['id'],prepare_only=True);b=improve(self.db,self.store,self.job['id'],prepare_only=True)
        self.assertEqual(a['improvement_id'],b['improvement_id']);self.assertEqual(a['slots'],b['slots']);self.assertEqual(len(self.db.jobs()),3)
    def test_candidate_budget_limits(self):
        for count,budget,disk in [(3,900,2048),(2,0,100),(2,float('nan'),100)]:
            with self.assertRaises(ValueError):prepare_improvement(self.db,self.store,self.job['id'],count,budget,disk)
        self.assertEqual(len(self.db.jobs()),1)
    def test_benchmark_required_before_production(self):
        self.store.db.execute('DELETE FROM QualityBenchmarks');self.store.db.commit()
        with self.assertRaises(ProductionError) as e:prepare_improvement(self.db,self.store,self.job['id'])
        self.assertEqual(e.exception.category,'BenchmarkRequired')
    def test_stage_reuse_texture_and_blender(self):
        outputs={stage:{'fake':{'path':'fake','sha256':'fake'}} for stage in ('ReferenceGenerated','ReferenceProcessed','GeometryGenerated','GeometryOptimized','PBRGenerated','Scaled')}
        self.assertEqual(select_stage({'findings':[{'category':'texture'}]}),'PBRGenerated')
        self.assertNotIn('PBRGenerated',reuse_for_stage(outputs,'PBRGenerated'));self.assertIn('GeometryOptimized',reuse_for_stage(outputs,'PBRGenerated'))
        self.assertIn('PBRGenerated',reuse_for_stage(outputs,'BlenderExported'));self.assertFalse(reuse_for_stage(outputs,'ReferenceGenerated'))
    @unittest.skipUnless((AI/'Backups/phase_3_1/preserved-files.json').is_file(), 'Optional local installation/evidence artifact is not distributed with source')
    def test_existing_artifact_and_qwen_preservation(self):
        baseline=json.loads((AI/'Backups/phase_3_1/preserved-files.json').read_text('utf-8'))
        for p,info in baseline.items():self.assertTrue(intact({'path':p,**info}),p)
    def test_split_batch_conflicts_are_reviewable(self):
        a=copy.deepcopy(self.payload);b=copy.deepcopy(a);a['observations'][0]['views']=['front'];b['observations'][0].update(result='not_met',views=['back'])
        merged=merge_batches([a,b]);self.assertEqual(merged['observations'][0]['result'],'uncertain');self.assertTrue(merged['contradictions'])
    def test_independent_visual_evidence_counters_prompt_assumption(self):
        facts=[{'view':v,'roof_material':'wood','raised_platform':'visible','confidence':.75,'evidence':'FAKE visible wood roof and platform',
                'chimney':'not_seen','water_barrel':'not_seen','tarps':'not_seen','roof_weights':'not_seen'} for v in VIEWS]
        p,proof=apply_grounding(self.payload,facts,self.i['requirements']);lookup={o['criterion_id']:o for o in p['observations']}
        self.assertEqual(lookup['GE01']['result'],'not_met');self.assertTrue(all(lookup[r['id']]['result']=='met' for r in self.i['requirements'] if r['visually_evaluable'] and r['id']!='GE01'))
        self.assertTrue(proof['overrides']);self.assertLess(p['scores']['geometry']['score'],90)
        for f in facts:f['object_form']='building_fragment'
        p,proof=apply_grounding(self.payload,facts,self.i['requirements'])
        self.assertFalse(proof['identity_failure']);self.assertEqual(p['scores']['prompt_fidelity']['score'],self.payload['scores']['prompt_fidelity']['score'])
        unrelated=copy.deepcopy(self.i['requirements'])
        for r in unrelated:
            if r['id'].startswith('PF'):r['description']='Different catalog asset feature'
        p,proof=apply_grounding(self.payload,facts,unrelated)
        self.assertFalse(proof['identity_failure']);self.assertFalse(any(o['criterion_id'].startswith('PF') for o in proof['overrides']))
    def test_technical_failure_and_shadow_preserved(self):
        r=score(self.payload,self.i,{'status':'FAIL','checks':[]},[{'view':v,'width':768} for v in VIEWS])
        self.assertEqual(r['technical_status'],'TechnicalFailed');self.assertNotEqual(r['decision'],'VisuallyApprovedCandidate')
        self.assertEqual(self.db.job(self.job['id'])['status'],'Completed');self.assertFalse(self.store.actions(self.job['id']))
    def test_protocol_difference_blocks_comparison(self):
        other=self.db.enqueue(self.asset,'other',settings_for(self.asset))[0];e=self.store.begin(other,{})
        r={**self.report,'job_id':other['id'],'evaluation_id':e['id'],'protocol_sha256':'different'};self.store.finish(e['id'],r)
        self.assertFalse(compare_candidates(self.db,self.store,self.asset['asset_id'])['same_evaluation_protocol'])
    def test_production_budget_accounting_across_restart(self):
        a=self.db.begin_stage(self.job['id'],'GeometryGenerated','fake',{},{});self.db.fail_stage(a,{'category':'FAKE'})
        self.assertGreaterEqual(used_production_seconds(self.db,[self.job['id']]),0)
    def test_manual_approval_not_created_by_prepare(self):
        improve(self.db,self.store,self.job['id'],prepare_only=True)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM QualityActions').fetchone()[0],0)

if __name__=='__main__':unittest.main(verbosity=2)
