"""Only the managed isolated pilot is writable. Real Editor JSON is required."""
import json
import uuid
from pathlib import Path
import numpy as np
from .common import AI,ROOT,ProductionError,ProcessLock,atomic_json,digest,file_info,intact
from .quality_gates import outputs_for
from .phase35_store import lookup,record
from .processes import run_process

PILOT=AI/'UnrealPilot/Phase35'
ENGINE=Path('C:/Program Files/Epic Games/UE_5.7')
SCRIPT=AI/'Scripts/phase35_unreal.py'


def isolated_project(path,allowed_root=None):
    path=Path(path).resolve();allowed=Path(allowed_root or PILOT).resolve()
    if path!=allowed or not path.is_relative_to((allowed.parent).resolve()):raise ProductionError('UnrealIsolation','Only the exact managed isolated pilot project is allowed')
    marker=path/'.game3dcreator-pilot.json'
    if not marker.is_file() or json.loads(marker.read_text('utf-8')).get('owner')!='game3dcreator-phase35':raise ProductionError('UnrealIsolation','Managed pilot marker missing; no external project writes')
    return path


def validate_engine_report(value,expected,mode):
    checks=[]
    def c(name,passed,detail=None):checks.append({'check':name,'status':'PASS' if passed else 'FAIL','detail':detail})
    c('real_editor_api',value.get('evidence_origin')=='unreal_editor_python_api' and str(value.get('engine_version','')).startswith('5.7.'))
    c('isolated_project',Path(value.get('project','')).resolve()==Path(expected['project']).resolve())
    c('source_manifest',value.get('source_manifest_sha256')==digest(expected['sources']))
    c('static_mesh_valid',value.get('mesh_class')=='StaticMesh' and value.get('lod_count',0)>=1)
    c('three_lods',value.get('lod_count')==3,value.get('lod_triangles'))
    triangles=value.get('lod_triangles',[]);c('lod_reduction',len(triangles)==3 and triangles[0]>=triangles[1]>=triangles[2]>0,triangles)
    actual=np.asarray(value.get('dimensions_cm',[]),float);target=np.asarray(expected['dimensions_m'],float)*100
    c('physical_scale_cm',actual.shape==(3,) and np.allclose(actual,target,atol=.7),{'actual':actual.tolist(),'expected':target.tolist()})
    c('within_requested_footprint',actual.shape==(3,) and np.all(actual[:2]<=target[:2]+.7),actual.tolist())
    pivot=value.get('bounds_min_cm',[]);c('ground_pivot',len(pivot)==3 and abs(pivot[2])<.7 and abs(value.get('bounds_origin_cm',[999,999])[0])<.7 and abs(value.get('bounds_origin_cm',[999,999])[1])<.7,pivot)
    c('axis_and_actor_transform',value.get('import_settings',{}).get('convert_scene') is True and value.get('import_settings',{}).get('convert_scene_unit') is True and value.get('actor_scale')==[1.0,1.0,1.0],value.get('import_settings'))
    c('simple_collision',value.get('simple_collision_count',0)>0,value.get('simple_collision_count'))
    slots=value.get('material_slots',[]);c('assigned_material',bool(slots) and all(s==expected['material_path'] for s in slots),slots)
    material=value.get('material',{})
    for name in ['albedo','roughness','metallic']:
        item=material.get(name,{})
        c(name+'_connection',item.get('connected_texture')==expected['texture_paths'][name],item)
        c(name+'_srgb',item.get('srgb')==(name=='albedo'),item.get('srgb'))
    c('saved_assets',value.get('saved') is True)
    if mode=='reload':c('independent_process_reload',value.get('mode')=='reload' and value.get('import_performed') is False)
    checks.append({'check':'editor_process_exit','status':'PASS' if value.get('process_exit_success',True) else 'WARN','detail':value.get('process_error')})
    return {'status':'FAIL' if any(i['status']=='FAIL' for i in checks) else 'WARN' if any(i['status']=='WARN' for i in checks) else 'PASS','checks':checks}


def idle_gpu():
    import requests
    from .quality_vlm import check_vlm_reservation
    check_vlm_reservation()
    for port in [8190,8191]:
        q=requests.get(f'http://127.0.0.1:{port}/queue',timeout=10);q.raise_for_status();q=q.json()
        if q.get('queue_running') or q.get('queue_pending'):raise ProductionError('GPUBusy','Unreal waits for both Comfy queues; no foreign jobs stopped')
    resident=requests.get('http://127.0.0.1:11434/api/ps',timeout=10);resident.raise_for_status()
    if resident.json().get('models'):raise ProductionError('GPUBusy','Unreal waits for Ollama unload; foreign model not stopped')


def validate_screenshots(render,pictures):
    from PIL import Image
    checks=[]
    def c(name,passed,detail=None):checks.append({'check':name,'status':'PASS' if passed else 'FAIL','detail':detail})
    c('actual_lit_viewport','VMI_LIT' in render.get('viewport_view_mode',''),render.get('viewport_view_mode'))
    c('three_forced_lod_captures',render.get('forced_lod_models')==[1,2,3] and len(pictures)==3)
    hashes=[]
    for picture in pictures:
        with Image.open(picture['path']) as im:
            rgb=np.asarray(im.convert('RGB'));coverage=float(np.mean(rgb.max(axis=2)>30))
            c('square_nonempty_'+Path(picture['path']).stem,im.size==(1024,1024) and float(rgb.std())>3 and coverage>.15,{'size':list(im.size),'non_dark_fraction':coverage})
            hashes.append(picture['sha256'])
    c('distinct_lod_images',len(hashes)==3 and len(set(hashes))==3)
    return {'status':'PASS' if all(i['status']=='PASS' for i in checks) else 'WARN','checks':checks}


class UnrealPilotService:
    def __init__(self,db,store,runner=run_process,project=None,engine=None):
        self.db=db;self.store=store;self.runner=runner;self.project=Path(project or PILOT);self.engine=Path(engine or ENGINE)

    def manifest(self,job_id):
        project=isolated_project(self.project);job=self.db.job(job_id);out=outputs_for(self.db,job_id)
        source={name:out[stage][key] for name,stage,key in [('fbx','BlenderExported','fbx'),('lod1','BlenderExported','lod1'),('lod2','BlenderExported','lod2'),
            ('model','Scaled','model'),('albedo','PBRGenerated','albedo'),('roughness','PBRGenerated','roughness'),('metallic','PBRGenerated','metallic')]}
        if not all(intact(i) for i in source.values()):raise ProductionError('ArtifactIntegrity','Existing export/PBR changed')
        report=json.loads(Path(out['BlenderExported']['blender_report']['path']).read_text('utf-8'))
        revision=file_info(SCRIPT)['sha256'][:12]
        # A fresh versioned namespace avoids UE reimport silently restoring old
        # Interchange/default unit options; earlier pilot packages stay intact.
        folder='/Game/LocalAssetStudio/Pilot/'+job['id']+'/'+revision
        return {'version':'unreal-pilot-3.5.0','job_id':job_id,'project':str(project),'sources':source,'dimensions_m':report['meshes'][0]['dimensions_m'],
            'mesh_path':folder+'/SM_PilotAsset','material_path':folder+'/M_PilotAsset',
            'texture_paths':{n:folder+'/T_PilotAsset_'+{'albedo':'Albedo','roughness':'Roughness','metallic':'Metallic'}[n] for n in ['albedo','roughness','metallic']},
            'level_path':'/Game/Phase35/L_Phase35_Pilot_'+revision,'automatic_approval':False,'main_project_modified':False}

    def _run(self,manifest,mode,work,render=False):
        config={**manifest,'mode':mode,'execution_nonce':uuid.uuid4().hex,'result_path':str(work/(mode+'-editor.json')),'screenshot_dir':str(work/'screenshots')}
        path=work/(mode+'-manifest.json');atomic_json(path,config)
        exe=self.engine/'Engine/Binaries/Win64/UnrealEditor.exe'
        if not exe.is_file():raise ProductionError('UnrealUnavailable','Installed engine binary missing: '+str(exe))
        # A tiny owned bootstrap passes an immutable JSON config to the shared script.
        bootstrap=work/(mode+'-bootstrap.py');bootstrap.write_text('import os,runpy\nos.environ["GAME3D_PILOT_MANIFEST"]='+repr(str(path))+'\nrunpy.run_path('+repr(str(SCRIPT))+',run_name="__main__")\n','utf-8')
        cmd=[exe,self.project/'Game3DCreatorPilot.uproject','-unattended','-nop4','-nosplash','-nosound','-NoLiveCoding','-nocef','-dx12','-stdout','-FullStdOutLogOutput']
        # StaticMeshEditorSubsystem is absent in Python commandlets in this engine.
        # All three independent sessions use a real offscreen Editor/RHI context.
        cmd+=['-RenderOffscreen','-windowed','-ResX=1024','-ResY=1024','-ExecutePythonScript='+str(bootstrap)]
        process_error=None
        try:self.runner(cmd,work,600,label='unreal-'+mode)
        except ProductionError as e:process_error={'category':e.category,'message':str(e)}
        result=work/(mode+'-editor.json')
        if not result.is_file():raise ProductionError('UnrealEvidenceMissing','Editor exited without real API evidence')
        value=json.loads(result.read_text('utf-8'))
        if value.get('error'):raise ProductionError('UnrealAPI',json.dumps(value['error']))
        if value.get('execution_nonce')!=config['execution_nonce'] or value.get('evidence_origin')!='unreal_editor_python_api':raise ProductionError('UnrealEvidenceMissing','Fresh per-execution real Editor nonce required')
        value['process_exit_success']=process_error is None
        if process_error:value['process_error']=process_error
        return value

    def pilot_import(self,job_id,render=True):
        manifest=self.manifest(job_id);key=digest({'manifest':manifest,'script':file_info(SCRIPT)['sha256']})
        old=lookup(self.store,'unreal',key)
        if old:
            if any(not intact(i) for i in old.get('artifacts',[])):raise ProductionError('ArtifactIntegrity','Pilot evidence changed; no automatic reimport')
            return old
        work=self.store.output_root/'Unreal'/job_id/key;work.mkdir(parents=True,exist_ok=True)
        r={'job_id':job_id,'status':'BLOCKED','manifest':manifest,'automatic_approval':False,'work_dir':str(work)}
        with ProcessLock(self.store.path.with_suffix('.unreal.lock')),ProcessLock(AI/'State/production.lock'):
            try:
                idle_gpu()
                imported=self._run(manifest,'import',work);reloaded=self._run(manifest,'reload',work)
                r.update(import_result=imported,reload_result=reloaded,validation=validate_engine_report(reloaded,manifest,'reload'))
                r['import_validation']=validate_engine_report(imported,manifest,'import')
                statuses=[r['validation']['status'],r['import_validation']['status']];r['status']='FAIL' if 'FAIL' in statuses else 'WARN' if 'WARN' in statuses else 'PASS'
                if render:
                    r['render_result']=self._run(manifest,'render',work,render=True)
                    pictures=[file_info(p) for p in (work/'screenshots').glob('*.png')]
                    r['screenshot_evidence']=pictures
                    r['screenshot_validation']=validate_screenshots(r['render_result'],pictures)
                    if r['screenshot_validation']['status']!='PASS' and r['status']!='FAIL':r['status']='WARN'
                    if not r['render_result'].get('process_exit_success'):r['status']='WARN' if r['status']!='FAIL' else 'FAIL'
            except Exception as e:r.update(status='BLOCKED' if 'reload_result' not in r else 'WARN',error={'category':getattr(e,'category',type(e).__name__),'message':str(e)})
        r['artifacts']=[file_info(p) for p in work.rglob('*') if p.is_file()]
        atomic_json(work/'unreal-validation.json',r);record(self.store,'unreal',key,r);return r

    def validate(self,job_id):
        rows=[json.loads(r[0]) for r in self.store.db.execute("SELECT report_json FROM Phase35Records WHERE kind='unreal' ORDER BY created_at DESC")]
        value=next((r for r in rows if r['job_id']==job_id),None)
        if not value:return {'status':'BLOCKED','reason':'No real Editor pilot report'}
        if any(not intact(i) for i in value.get('artifacts',[])):raise ProductionError('ArtifactIntegrity','Pilot report artifacts changed')
        return value
