"""Thirty isolated contracts. Synthetic masks/meshes and fake Editor JSON are not real AI/UE evidence."""
import copy,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import trimesh
from PIL import Image,ImageDraw
from tests.runtime import TEST_ROOT
from ai3d_manager.catalog import load_catalog
from ai3d_manager.common import ProductionError,atomic_json,file_info,intact
from ai3d_manager.category_pipeline import load_profiles,resolve_profile,active_provider,apply_profile,recommend
from ai3d_manager.image_providers import provider_id,load_registry
from ai3d_manager.segmentation import SegmentationStrategy,SegmentationService,mask_metrics,rank_candidate
from ai3d_manager.phase35_store import open_state,record,lookup,claim_inference
from ai3d_manager.worker import Worker,settings_for
from ai3d_manager.detail_refinement import classify,validate_attachment,validate_transform,load_in_blender_space
from ai3d_manager.unreal_pilot import isolated_project,validate_engine_report,validate_screenshots,idle_gpu,UnrealPilotService


class Phase35Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_ROOT);self.root=Path(self.temp.name);self.db,self.store=open_state(self.root)
        self.assets={a['asset_id']:a for a in load_catalog()};self.asset=self.assets['DemoTree'];self.config=load_profiles()
        self.png=self.root/'FAKE-tree.png';im=Image.new('RGB',(160,160),'white');draw=ImageDraw.Draw(im)
        draw.rectangle((72,70,88,145),fill=(90,50,20));draw.ellipse((18,12,142,110),fill=(40,100,25));im.save(self.png)
        rgba=im.convert('RGBA');rgba.putalpha(Image.fromarray(np.where(np.array(im).min(axis=2)<240,255,0).astype('uint8')));self.rgba=self.root/'FAKE-tree-rgba.png';rgba.save(self.rgba)
    def tearDown(self):
        self.store.close();self.db.close()
        self.assertTrue(self.root.resolve().is_relative_to(TEST_ROOT.resolve()))
        for i in range(8):
            try:self.temp.cleanup();break
            except OSError:
                if i==7:raise
                time.sleep(.1*(i+1))
    def source_job(self,asset=None):
        a={**(asset or self.asset),'production_role':'reference_candidate'};self.db.sync_assets([a]);settings=settings_for(a);settings['reuse_files']={'ReferenceGenerated':{'reference':file_info(self.png)},'ReferenceProcessed':{'preprocessed':file_info(self.rgba)}}
        job,_=self.db.enqueue(a,'FAKE-source',settings);return Worker(self.db).run(job['id'])
    def service(self,status='PASS'):
        class Gate:
            def __init__(self,d,s):pass
            def run(self,kind,job):return {'status':status,'FAKE':True,'semantic_identity':'fake fixture verdict'}
        return SegmentationService(self.db,self.db,self.store,Gate)
    def ue(self):
        expected={'project':str(self.root),'dimensions_m':[8,7.885,4.037],'sources':{},'material_path':'/Game/Phase35/M','texture_paths':{n:'/Game/Phase35/'+n for n in ['albedo','roughness','metallic']}}
        from ai3d_manager.common import digest
        value={'evidence_origin':'unreal_editor_python_api','engine_version':'5.7.4-FAKE','project':str(self.root),'source_manifest_sha256':digest({}),
            'mesh_class':'StaticMesh','lod_count':3,'lod_triangles':[8000,4000,2000],'dimensions_cm':[800,788.5,403.7],
            'bounds_min_cm':[-400,-394.25,0],'bounds_origin_cm':[0,0,201.85],
            'import_settings':{'convert_scene':True,'convert_scene_unit':True},'actor_scale':[1.,1.,1.],
            'simple_collision_count':1,'material_slots':[expected['material_path']],
            'material':{n:{'connected_texture':expected['texture_paths'][n],'srgb':n=='albedo'} for n in expected['texture_paths']},'saved':True,'mode':'reload','import_performed':False,'FAKE':True}
        return expected,value
    def attachment(self):
        p=self.root/'FAKE-box.glb';trimesh.creation.box().export(p)
        position=load_in_blender_space(p).triangles[0].mean(axis=0).tolist()
        verification=self.root/'FAKE-verification.json';atomic_json(verification,{'source_mesh':file_info(p),'decision':'HumanReviewed','reviewed_by':'FAKE fixture; not production evidence'})
        return {'coordinate_system':'blender_z_up_meters','parent_asset':file_info(p),'accessory_asset':file_info(p),'attachment_point':{'parent_sha256':file_info(p)['sha256'],'triangle_index':0,'barycentric':[1/3,1/3,1/3],'semantic_surface':'FAKE roof','reviewed_by':'fixture','surface_evidence':'FAKE test only'},
            'relative_transform':{'translation_m':position,'rotation_degrees':[0,0,0],'scale':[1,1,1]},'collision_relationship':'independent_simple','material_mapping':{},
            'provenance':{'source_kind':'verified_existing_asset','placeholder':False,'reviewed_by':'fixture','review_evidence':'FAKE fixture only','verification_report':file_info(verification)}}
    def test_01_category_recommendation(self):
        self.assertIsNone(recommend('Buildings')['recommendation']['preferred_provider']);self.assertIsNone(active_provider(self.assets['DemoCabin'],{}));self.assertEqual(self.config['asset_profiles'],{})
        with self.assertRaises(ValueError):resolve_profile(self.assets['DemoFigure'])
    def test_02_provider_override_precedence(self):
        c=copy.deepcopy(self.config);c['active']=True;c['profiles']['Organic']['preferred_provider']='z-image-turbo'
        with patch('ai3d_manager.category_pipeline.load_profiles',return_value=c):
            self.assertEqual(provider_id(self.asset,{}),'z-image-turbo');self.assertEqual(provider_id(self.asset,{'image_provider':'qwen-image-2512'}),'qwen-image-2512')
        with self.assertRaises(ValueError):active_provider(self.asset,{'category_profile':'Buildings'},c)
    def test_03_strategy_selection(self):
        a,o=apply_profile(self.asset,{'category_profile':'auto'});self.assertEqual(o['segmentation_strategy'],'adaptive-border');self.assertEqual(a['phase35_gate_profile'],'tree')
    def test_04_alpha_preservation(self):
        p=self.root/'alpha.png';SegmentationStrategy('alpha-preservation').apply(self.rgba,p,trusted_alpha=True);self.assertEqual(file_info(p)['sha256'],file_info(self.rgba)['sha256'])
        with self.assertRaises(ProductionError):SegmentationStrategy('alpha-preservation').apply(self.png,p,trusted_alpha=True)
    def test_05_u2net_fallback(self):
        p=self.root/'u2.png';r=SegmentationStrategy('existing-u2net').apply(self.png,p,existing_rgba=file_info(self.rgba));self.assertEqual(r['mode'],'copied_existing_u2net_result')
        with patch('ai3d_manager.segmentation.AI',self.root/'missing'):
            with self.assertRaises(ProductionError):SegmentationStrategy('existing-u2net').apply(self.png,p)
    def test_06_candidate_isolation(self):
        j=self.source_job();r=self.service().evaluate(j['id'],['adaptive-border','adaptive-grabcut']);paths=[x['rgba']['path'] for x in r['candidates']];self.assertEqual(len(set(paths)),2);self.assertNotIn(str(self.png),paths)
    def test_07_semantic_preservation(self):
        j=self.source_job();r=self.service('FAIL').evaluate(j['id'],['adaptive-border']);self.assertIsNone(r['selected_candidate_id']);self.assertEqual(r['candidates'][0]['gate']['status'],'FAIL')
    def test_08_canopy_preservation(self):
        im=Image.open(self.rgba);a=np.array(im);a[:85,:,3]=0;p=self.root/'lost.png';Image.fromarray(a).save(p)
        bad=mask_metrics(self.png,p,True);SegmentationStrategy('adaptive-border').apply(self.png,p);good=mask_metrics(self.png,p,True)
        self.assertLess(bad['source_color_evidence_coverage']['canopy'],.1);self.assertGreater(good['source_color_evidence_coverage']['canopy'],.95)
    def test_09_false_foreground(self):
        p=self.root/'opaque.png';Image.open(self.png).convert('RGBA').save(p);m=mask_metrics(self.png,p);self.assertGreater(m['background_like_foreground_fraction'],.2)
        im=Image.open(self.png);a=np.array(im);a[125:145,115:150]=[215,215,215];im=Image.fromarray(a);im.save(self.root/'shadow.png')
        SegmentationStrategy('adaptive-grabcut').apply(self.root/'shadow.png',p,organic=True)
        self.assertLess((np.array(Image.open(p))[130:140,120:145,3]>127).mean(),.05)
    def test_10_candidate_comparison(self):
        a={'gate':{'status':'FAIL'},'status':'FAIL','metrics':{'foreground_occupancy':.9}};b={'gate':{'status':'PASS'},'status':'PASS','metrics':{'foreground_occupancy':.2}}
        self.assertGreater(rank_candidate(b),rank_candidate(a))
    def test_11_reference_gate_integration(self):
        j=self.source_job();r=self.service().evaluate(j['id'],['adaptive-border']);self.assertTrue(r['candidates'][0]['gate']['FAKE']);self.assertEqual(self.db.job(r['candidates'][0]['child_job_id'])['status'],'ReferenceReady')
    def test_12_identity_profiles(self):
        from ai3d_manager.gate_analysis import profile_for
        self.assertEqual(profile_for({**self.asset,'phase35_gate_profile':'tree'})['name'],'tree');self.assertEqual(resolve_profile(self.assets['DemoMarker'])['name'],'Buildings');self.assertEqual(resolve_profile(self.assets['DemoPile'])['name'],'Debris')
        with self.assertRaises(ValueError):resolve_profile({'asset_id':'Tree'})
    def test_13_refinement_planner(self):
        from types import SimpleNamespace
        from ai3d_manager.detail_refinement import AssetDetailRefinementPlanner
        self.assertEqual(classify({'description':'Wrong building identity'}),'reference_regeneration')
        p=self.root/'FAKE-body.glb';trimesh.creation.box(extents=[1,2,3]).export(p);source=file_info(p)
        d=SimpleNamespace(job=lambda j:{'id':j},stages=lambda j:[{'name':'Scaled','status':'Completed','id':'s'}],artifact_dict=lambda s:{'model':source})
        visual={'overall_score':50,'requirements':[{'id':'FAKE01','description':'Small chimney pipe','result':'uncertain','source':{'quote':'chimney'},'evidence':'FAKE occluded fixture','views':['front']},
            {'id':'NV','description':'Capacity','visually_evaluable':False,'result':'UnverifiableFromRenders','source':{'quote':'four people'}},
            {'id':'TM','description':'Footprint','visually_evaluable':False,'result':'PASS','source':{'quote':'8 meters'}}]}
        q=SimpleNamespace(latest=lambda j:{'id':'FAKE-eval','report_json':json.dumps(visual),'work_dir':str(self.root)},gate_report=lambda j,k:None)
        planner=AssetDetailRefinementPlanner(d,q,self.store);r=planner.plan('FAKE-job');self.assertFalse(r['parent_body_modified']);self.assertFalse(r['actions'][0]['applied']);self.assertEqual(planner.plan('FAKE-job'),r)
        self.assertEqual(len(r['actions']),2);self.assertNotIn('TM',[a['criterion_id'] for a in r['actions']])
        np.testing.assert_allclose(planner.analyze('FAKE-job')['dimensions_m'],[1,3,2]);self.assertEqual(r['coordinate_system'],'blender_z_up_meters')
    def test_14_texture_only(self):self.assertEqual(classify({'description':'Rust color roughness'}),'texture_only')
    def test_15_additive_geometry(self):self.assertEqual(classify({'description':'Small chimney pipe'}),'additive_geometry')
    def test_16_unsupported_edit(self):self.assertEqual(classify({'description':'Fused ground platform'}),'manual_review');self.assertEqual(classify({'description':'Hidden bedrolls'}),'manual_review')
    def test_17_attachment_metadata(self):
        value=self.attachment();self.assertEqual(validate_attachment(value),value);del value['material_mapping']
        with self.assertRaises(ValueError):validate_attachment(value)
    def test_18_transform_validation(self):
        t=self.attachment()['relative_transform'];t['scale'][0]=0
        with self.assertRaises(ValueError):validate_transform(t)
        t['scale'][0]=float('nan')
        with self.assertRaises(ValueError):validate_transform(t)
        v=self.attachment();v['relative_transform']['translation_m']=[10,10,10]
        with self.assertRaises(ProductionError):validate_attachment(v)
    def test_19_provenance(self):
        v=self.attachment();v['provenance']['placeholder']=True
        with self.assertRaises(ProductionError):validate_attachment(v)
        v=self.attachment();v['attachment_point']['surface_evidence']=''
        with self.assertRaises(ProductionError):validate_attachment(v)
    def test_20_pilot_isolation(self):
        atomic_json(self.root/'.game3dcreator-pilot.json',{'owner':'game3dcreator-phase35'});self.assertEqual(isolated_project(self.root,self.root),self.root)
        with self.assertRaises(ProductionError):isolated_project(Path('C:/UnmanagedGameProject'))
    def test_21_unreal_evidence(self):
        e,v=self.ue();self.assertEqual(validate_engine_report(v,e,'reload')['status'],'PASS');v['evidence_origin']='blender_render';self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
        fake_engine=self.root/'FAKE-engine';exe=fake_engine/'Engine/Binaries/Win64/UnrealEditor.exe';exe.parent.mkdir(parents=True);exe.write_bytes(b'FAKE not executable')
        service=UnrealPilotService(self.db,self.store,runner=lambda *a,**k:None,project=self.root,engine=fake_engine)
        with self.assertRaises(ProductionError):service._run(e,'import',self.root)
        pictures=[]
        for i in range(3):
            path=self.root/('FAKE-lod'+str(i)+'.png');im=Image.new('RGB',(1024,1024),'gray');ImageDraw.Draw(im).rectangle((100,100,300+i*50,400),fill='brown');im.save(path);pictures.append(file_info(path))
        render={'viewport_view_mode':'VMI_LIT','forced_lod_models':[1,2,3]}
        self.assertEqual(validate_screenshots(render,pictures)['status'],'PASS')
        self.assertEqual(validate_screenshots({**render,'viewport_view_mode':'VMI_WIREFRAME'},pictures)['status'],'WARN')
        self.assertEqual(validate_screenshots(render,[pictures[0]]*3)['status'],'WARN')
        Image.new('RGB',(1024,576),'black').save(pictures[0]['path'])
        self.assertEqual(validate_screenshots(render,pictures)['status'],'WARN')
    def test_22_material_mapping(self):
        e,v=self.ue();v['material']['roughness']['srgb']=True;self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
    def test_23_lods(self):
        e,v=self.ue();v['lod_count']=1;self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
    def test_24_collision(self):
        e,v=self.ue();v['simple_collision_count']=0;self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
    def test_25_scale_pivot(self):
        e,v=self.ue();v['dimensions_cm'][0]=8;self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
        e,v=self.ue();v['bounds_min_cm'][2]=-100;self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
    def test_26_reload(self):
        e,v=self.ue();v['import_performed']=True;self.assertEqual(validate_engine_report(v,e,'reload')['status'],'FAIL')
        e,v=self.ue();v['process_exit_success']=False;v['process_error']={'message':'FAKE shutdown crash'};self.assertEqual(validate_engine_report(v,e,'reload')['status'],'WARN')
    def test_27_gpu_sequencing(self):
        class Response:
            def raise_for_status(self):pass
            def json(self):return {'queue_running':[['foreign']],'queue_pending':[]}
        with patch('ai3d_manager.quality_vlm.check_vlm_reservation'),patch('requests.get',return_value=Response()):
            with self.assertRaises(ProductionError):idle_gpu()
        claim_inference(self.store,'shape','first','small_accessory')
        with self.assertRaises(ProductionError):claim_inference(self.store,'shape','retry','small_accessory')
        with self.assertRaises(ProductionError):claim_inference(self.store,'shape','building','building')
        with self.assertRaises(ProductionError):claim_inference(self.store,'reference','no-proof','small_accessory')
    def test_28_sqlite_persistence(self):
        record(self.store,'FAKE','key',{'status':'FAKE'});self.assertEqual(lookup(self.store,'FAKE','key'),{'status':'FAKE'})
    def test_29_preservation(self):
        before=file_info(self.png);j=self.source_job();self.service().evaluate(j['id'],['adaptive-border']);self.assertTrue(intact(before));self.assertEqual(self.db.job(j['id'])['status'],'ReferenceReady')
    def test_30_restart_idempotency(self):
        j=self.source_job();r=self.service().evaluate(j['id'],['adaptive-border']);n=len(self.db.jobs());attempts=self.db.db.execute('SELECT COUNT(*) FROM JobAttempts').fetchone()[0]
        again=self.service().evaluate(j['id'],['adaptive-border']);self.assertEqual(r,again);self.assertEqual(n,len(self.db.jobs()));self.assertEqual(attempts,self.db.db.execute('SELECT COUNT(*) FROM JobAttempts').fetchone()[0])


if __name__=='__main__':unittest.main()
