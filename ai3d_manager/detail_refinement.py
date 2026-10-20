"""Non-destructive analysis and guarded child-mesh assembly specifications."""
import json
import math
from pathlib import Path
import numpy as np
import trimesh
from .common import AI,ProductionError,atomic_json,digest,file_info,intact
from .quality_gates import outputs_for,current_gate
from .phase35_store import lookup,record

VERSION='detail-refinement-3.5.1'


def load_in_blender_space(path):
    if Path(path).suffix.lower() not in ['.glb','.gltf']:raise ValueError('Reviewed assembly currently requires glTF meter/Y-up sources')
    mesh=trimesh.load(path,force='scene').to_geometry()
    # glTF is Y-up; Blender glTF import applies the same +90deg X rotation.
    mesh.apply_transform([[1,0,0,0],[0,0,-1,0],[0,1,0,0],[0,0,0,1]])
    return mesh


def classify(criterion):
    text=(criterion.get('description','')+' '+criterion.get('suggested_fix','')).lower()
    if criterion.get('result')=='uncertain' and any(w in text for w in ['material','color','colour','texture']):return 'texture_only'
    if any(w in text for w in ['ground plane','platform','deformation','fused','base plinth','bedroll','interior']):return 'manual_review'
    if any(w in text for w in ['chimney','barrel','tire','accessor','curtain','tarps','canvas','panel']):return 'additive_geometry'
    if any(w in text for w in ['whole object','silhouette','identity','wrong building','main body','structural']):return 'reference_regeneration'
    if any(w in text for w in ['color','colour','rust','texture','roughness','metallic','material']):return 'texture_only'
    return 'manual_review'


def validate_transform(value):
    for k in ['translation_m','rotation_degrees','scale']:
        if k not in value or len(value[k])!=3 or any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in value[k]):raise ValueError('Finite XYZ transform required')
    if min(value['scale'])<=0 or max(value['scale'])>100:raise ValueError('Positive reviewed scale required')
    if max(abs(x) for x in value['translation_m'])>10000:raise ValueError('Attachment translation outside supported range')
    return value


def validate_attachment(value):
    required=['parent_asset','accessory_asset','attachment_point','relative_transform','collision_relationship','material_mapping','provenance']
    if any(k not in value for k in required):raise ValueError('Incomplete child-component metadata')
    validate_transform(value['relative_transform'])
    if value.get('coordinate_system')!='blender_z_up_meters':raise ValueError('Attachment transforms must explicitly use Blender Z-up meters')
    if value['collision_relationship'] not in ['independent_simple','inherit_parent','disabled_reviewed']:raise ValueError('Collision relationship must be explicit')
    if not isinstance(value['material_mapping'],dict):raise ValueError('Explicit material mapping required')
    for name in ['parent_asset','accessory_asset']:
        if not intact(value[name]):raise ProductionError('ArtifactIntegrity','Attachment source hash mismatch')
    proof=value['provenance']
    if proof.get('placeholder') or proof.get('source_kind') not in ['verified_existing_asset','independently_generated_mesh']:raise ProductionError('UnverifiedAccessory','Placeholder/unverified mesh cannot become a production accessory')
    if not proof.get('reviewed_by') or not proof.get('review_evidence'):raise ProductionError('AccessoryNeedsReview','Verified source and explicit artist review evidence required')
    if not intact(proof.get('verification_report',{})):raise ProductionError('UnverifiedAccessory','Hashed independent accessory verification report required')
    verification=json.loads(Path(proof['verification_report']['path']).read_text('utf-8'))
    if verification.get('source_mesh')!=value['accessory_asset'] or verification.get('decision') not in ['Approved','HumanReviewed'] or not verification.get('reviewed_by'):
        raise ProductionError('UnverifiedAccessory','Verification must cover this exact accessory mesh and explicit human review')
    anchor=value['attachment_point']
    if anchor.get('parent_sha256')!=value['parent_asset']['sha256'] or not anchor.get('semantic_surface') or not anchor.get('reviewed_by') or not anchor.get('surface_evidence'):
        raise ProductionError('AttachmentNeedsReview','A bounding box alone is not a reviewed roof/entrance surface')
    if not isinstance(anchor.get('triangle_index'),int) or anchor['triangle_index']<0:raise ValueError('Actual parent triangle anchor required')
    mesh=load_in_blender_space(value['parent_asset']['path'])
    if anchor['triangle_index']>=len(mesh.faces):raise ValueError('Attachment triangle outside parent mesh')
    bary=np.asarray(anchor.get('barycentric',[]),float)
    if bary.shape!=(3,) or not np.isfinite(bary).all() or min(bary)<0 or not np.isclose(bary.sum(),1):raise ValueError('Barycentric anchor on the reviewed triangle required')
    point=bary@mesh.triangles[anchor['triangle_index']]
    if not np.allclose(point,value['relative_transform']['translation_m'],atol=.02):raise ProductionError('AttachmentNeedsReview','Relative mount translation must contact the reviewed surface triangle')
    return value


def assemble_reviewed_attachment(value,work,blender):
    validate_attachment(value)
    work=Path(work).resolve()
    if not work.is_relative_to((AI/'Outputs/Phase35').resolve()):raise ValueError('Assembly export must stay in new Phase35 output directory')
    if work.exists():raise ProductionError('ArtifactExists','Assembly never overwrites a prior export')
    work.mkdir(parents=True);atomic_json(work/'attachment.json',value)
    from .processes import run_process
    run_process([blender,'--background','--python-exit-code','23','--python',AI/'Scripts/phase35_assemble.py','--',work/'attachment.json',work],work,300,label='attachment')
    return {'status':'NeedsReview','body_source_unchanged':intact(value['parent_asset']),
        'outputs':[file_info(work/n) for n in ['assembly.blend','assembly.glb','assembly-report.json']], 'automatic_approval':False}


class AssetDetailRefinementPlanner:
    def __init__(self,db,quality,store):self.db=db;self.quality=quality;self.store=store

    def analyze(self,job_id):
        job=self.db.job(job_id);out=outputs_for(self.db,job_id);source=out.get('Scaled',{}).get('model')
        if not source or not intact(source):raise ProductionError('ArtifactIntegrity','Intact final model required')
        evaluation=self.quality.latest(job_id)
        if not evaluation or not evaluation['report_json']:raise ProductionError('RefinementNeedsReview','Existing final evidence required')
        visual=json.loads(evaluation['report_json']);key=digest({'version':VERSION,'source':source,'visual':digest(visual)})
        old=lookup(self.store,'refinement_analysis',key)
        if old:return old
        scene=trimesh.load(source['path'],force='scene');mesh=load_in_blender_space(source['path'])
        centers=mesh.triangles_center;roof=(mesh.face_normals[:,2]>.6)&(centers[:,2]>mesh.bounds[0,2]+mesh.extents[2]*.65)
        roof_indices=np.flatnonzero(roof)
        report={'job_id':job_id,'version':VERSION,'source':source,'status':'NeedsReview','parent_body_modified':False,
            'baseline_evaluation_id':evaluation['id'],'baseline_score':visual['overall_score'],
            'geometry_gate':current_gate(self.db,self.quality,job_id,'geometry'),'triangles':len(mesh.faces),'dimensions_m':mesh.extents.tolist(),
            'bounds_m':mesh.bounds.tolist(),'uv_present':any(getattr(m.visual,'uv',None) is not None for m in scene.geometry.values()),
            'coordinate_system':'blender_z_up_meters','source_coordinate_system':'gltf_y_up_meters',
            'surface_candidates':{'upward_upper_triangles':int(roof.sum()),'sample_triangle_indices':roof_indices[:12].tolist(),
                'semantic_surface_verified':False,'automatic_attachment_allowed':False,
                'reason':'Actual upper, upward-facing faces measured; this does not prove a safe chimney opening or roof support.'},
            'requirements':visual['requirements'],'unverifiable_requirements':visual.get('unverifiable_requirements'),
            'source_visual_report':str(Path(evaluation['work_dir'])/'visual-validation.json'),'new_shape_required':False,'automatic_approval':False}
        work=self.store.output_root/'Refinement'/job_id/key;work.mkdir(parents=True,exist_ok=True);report['work_dir']=str(work)
        atomic_json(work/'analysis.json',report);record(self.store,'refinement_analysis',key,report);return report

    def plan(self,job_id):
        analysis=self.analyze(job_id);key=digest(analysis);old=lookup(self.store,'refinement_plan',key)
        if old:return old
        actions=[]
        for item in analysis['requirements']:
            if item['result'] in ['met','Met','PASS']:continue
            kind=classify(item)
            if not item.get('visually_evaluable',True):kind='manual_review'
            target=item['id']
            evidence=item.get('evidence') or 'Not observable in renders; requires nonvisual measurement or artist/user confirmation.'
            related={'criterion_id':item['id'],'source':item['source'],'observed_result':item['result'],'evidence':evidence}
            previous=next((a for a in actions if a['target']==target and a['kind']==kind),None)
            if previous:previous['related_criteria'].append(related);continue
            actions.append({'criterion_id':item['id'],'source':item['source'],'description':item['description'],
                'target':target,'related_criteria':[related],'observed_result':item['result'],'evidence':evidence,'views':item.get('views',[]),
                'kind':kind,'status':'NeedsReview','applied':False,
                'execution_support':'reviewed_child_mesh_assembly' if kind=='additive_geometry' else 'plan_only',
                'blockers':(['No verified independent accessory mesh','No semantically reviewed attachment surface'] if kind=='additive_geometry' else
                    ['No safe automatic UV-local texture edit executor'] if kind=='texture_only' else
                    ['New full building Shape is forbidden in Phase35'] if kind=='reference_regeneration' else ['Artist review or more revealing view required']),
                'hidden_detail_absence_asserted':False})
        result={'job_id':job_id,'version':VERSION,'source':analysis['source'],'baseline_score':analysis['baseline_score'],
            'coordinate_system':analysis['coordinate_system'],'analysis_report_path':str(Path(analysis['work_dir'])/'analysis.json'),
            'status':'NeedsReview','actions':actions,'parent_body_modified':False,'new_shape_submitted':False,'new_pbr_submitted':False,
            'automatic_approval':False,'accessory_policy':'user_supplied_verified_mesh',
            'attachment_schema':{'parent_asset':'hashed model','accessory_asset':'hashed verified mesh','attachment_point':'reviewed semantic surface + actual triangle index + parent hash',
                'relative_transform':['translation_m','rotation_degrees','scale'],'collision_relationship':'explicit','material_mapping':'explicit','provenance':'source kind, review evidence, reviewed_by'},
            'reason':'Current whole body is correct; preserve it. Missing/uncertain details are plans, not completed edits.'}
        result['manual_review_checklist']=['Confirm requested details in sufficiently revealing views.',
            'Capacity and other nonvisual facts require independent verification.',
            'Missing visibility is uncertainty, not proof of absence.']
        atomic_json(Path(analysis['work_dir'])/'plan.json',result);record(self.store,'refinement_plan',key,result);return result
