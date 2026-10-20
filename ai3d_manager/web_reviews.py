"""Human model decisions, independent of production and immutable model files."""
import json
from pathlib import Path
from .common import canonical,utcnow,intact,file_info

MODEL_CHOICES=[('Packaged','model.glb','Teslim modeli'),('Scaled','model','Ölçeklenmiş model'),
               ('PBRGenerated','pbr','PBR modeli'),('GeometryOptimized','optimized','Optimize geometri'),
               ('GeometryGenerated','geometry','Ham geometri')]


def best_model(outputs):
    for stage,name,label in MODEL_CHOICES:
        info=outputs.get(stage,{}).get(name)
        if info:return {'stage':stage,'name':name,'label':label,'info':info}
    return None


def history(db,subject):
    return [{**dict(r),'model':json.loads(r['model_json'])} for r in db.db.execute(
        'SELECT * FROM WebModelReviews WHERE subject=? ORDER BY id DESC',(subject,))]


def append_review(db,subject,action,note,model):
    db.db.execute('INSERT INTO WebModelReviews(subject,action,note,model_json,created_at) VALUES (?,?,?,?,?)',
                  (subject,action,note,canonical(model or {}),utcnow()))
    return db.db.execute('SELECT last_insert_rowid()').fetchone()[0]


def read_report(info):
    if not info:return None
    if not intact(info):return None
    try:return json.loads(Path(info['path']).read_text('utf-8'))
    except (OSError,ValueError):return None


def web_subject(db,ident):
    from .web_control import control,snapshot
    job=db.job(ident);row=control(db,ident);out=snapshot(db,ident);asset=json.loads(job['request_json'])
    model=best_model(out)
    if not model:raise ValueError('Bu işin henüz 3D model dosyası yok.')
    path=Path(model['info']['path']).resolve()
    if not path.is_relative_to(Path(job['output_dir']).resolve()):raise ValueError('Model yolu iş klasörünün dışında.')
    preview=out.get('PreviewsRendered',{}).get('preview_front') or out.get('ReferenceGenerated',{}).get('reference')
    preview_stage='PreviewsRendered' if 'preview_front' in out.get('PreviewsRendered',{}) else 'ReferenceGenerated'
    technical=read_report(out.get('Validated',{}).get('validation'))
    optimized=read_report(out.get('GeometryOptimized',{}).get('optimization_report')) or {}
    scale=read_report(out.get('Scaled',{}).get('scale_report')) or {}
    return {'subject':'web:'+ident,'id':ident,'name':asset.get('name_tr',job['asset_id']),'asset_id':job['asset_id'],
            'source':'Web üretimi','created_at':job['created_at'],'revision':row['revision'],
            'control':row,'outputs':out,'model':model,'technical':technical,
            'triangles':optimized.get('output_faces'),'dimensions':scale.get('actual_width_height_depth_m'),
            'thumbnail':f'/files/job/{ident}/{preview_stage}/'+('preview_front' if preview_stage=='PreviewsRendered' else 'reference') if preview else None,
            'reference_url':f'/files/job/{ident}/ReferenceGenerated/reference' if out.get('ReferenceGenerated',{}).get('reference') else None,
            'can_accept':row['state'] not in ('Running','Queued'),
            'acceptance_scope':'delivery' if row['cursor']>=2 and model['stage']=='Packaged' and row['state'] in ('Waiting','UserAccepted') else 'model_only',
            'can_decide':row['state'] not in ('Running','Queued'),
            'production_error':row['error'],'download_url':f'/api/jobs/{ident}/download' if row['cursor']>=2 else None,
            'prompt':asset['prompt']}


def library_subject(item,directory):
    root=Path(directory).resolve()/item['id'];path=root/'model.glb'
    if not root.is_relative_to(Path(directory).resolve()) or not path.is_file():raise ValueError('Bu kütüphane varlığının 3D dosyası yok.')
    model={'stage':'Library','name':'model.glb','label':'Kütüphane teslim modeli','info':file_info(path)}
    validation=root/'validation.json';technical=read_report(file_info(validation)) if validation.is_file() else None
    return {'subject':'library:'+item['id'],'id':item['id'],'name':item['name'],'asset_id':item['id'],
            'source':'Kütüphane','created_at':'','revision':0,'control':None,'outputs':{},'model':model,'technical':technical,
            'triangles':item.get('triangles'),'dimensions':None,'thumbnail':item['preview'],
            'reference_url':f'/files/library/{item["id"]}/reference.png' if (root/'reference.png').is_file() else None,
            'can_accept':True,'acceptance_scope':'model_only' if item['status']=='Blocked' or not technical or technical.get('status')=='FAIL' else 'delivery',
            'can_decide':True,'production_error':item.get('reason'),'download_url':None,'prompt':''}


def decorate(db,entry):
    events=history(db,entry['subject']);latest=events[0] if events else None
    matching=next((r for r in events if r['model'].get('info',{}).get('sha256')==entry['model']['info']['sha256']),None)
    status=matching['action'] if matching else 'Pending'
    if not matching and entry['control'] and entry['control']['state']=='UserAccepted':
        # Older studio approvals predate the review page. Import their meaning
        # only for the exact same model bytes, without rewriting old records.
        approval=db.db.execute("SELECT snapshot_json FROM WebApprovals WHERE job_id=? AND target='UserAccepted' ORDER BY id DESC LIMIT 1",(entry['id'],)).fetchone()
        if approval:
            approved=best_model(json.loads(approval[0]))
            if approved and approved['info']['sha256']==entry['model']['info']['sha256']:status='Accepted'
    task=db.db.execute('SELECT review_id,child_job_id,state,error FROM WebReviewProductions WHERE subject=? ORDER BY review_id DESC LIMIT 1',(entry['subject'],)).fetchone()
    return {**entry,'review_status':status,'review_version':latest['id'] if latest else 0,'history':events,
            'revision_production':dict(task) if task else None}


def decide(db,entry,data):
    if not isinstance(data,dict):raise ValueError('Geçerli inceleme kararı gerekli.')
    action=data.get('action');note=data.get('note','')
    if action not in ('Accepted','RevisionRequested','Rejected'):raise ValueError('Geçersiz inceleme kararı.')
    if not isinstance(note,str) or len(note)>4000:raise ValueError('Not en fazla 4000 karakter olmalı.')
    note=note.strip()
    if action in ('RevisionRequested','Rejected') and not note:raise ValueError('Revizyon veya ret nedenini yazın.')
    if type(data.get('revision')) is not int or type(data.get('review_version')) is not int:raise ValueError('Güncel ekran sürümü gerekli.')
    db.db.execute('BEGIN IMMEDIATE')
    try:
        # The caller rebuilds the entry under the same app mutex. Also compare
        # durable versions inside SQLite, protecting multiple processes/tabs.
        current=history(db,entry['subject']);version=current[0]['id'] if current else 0
        row=None
        if entry['control']:
            from .web_control import control
            row=control(db,entry['id'])
        if version!=data['review_version'] or (row['revision'] if row else 0)!=data['revision']:
            raise ValueError('İnceleme başka bir sekmede değişti. Güncel modeli yeniden yükleyin.')
        if row and row['state'] in ('Running','Queued'):raise ValueError('Üretim çalışırken karar kaydedilemez; bitmesini bekleyin.')
        if data.get('model_sha256')!=entry['model']['info']['sha256'] or not intact(entry['model']['info']):
            raise ValueError('İncelediğiniz model değişmiş veya dosyası eksik. Onay kaydedilmedi.')
        if action=='Accepted' and not entry['can_accept']:raise ValueError('Model şu anda değişiyor; çalışan işlemin bitmesini bekleyin.')
        ident=append_review(db,entry['subject'],action,note,entry['model'])
        if action in ('RevisionRequested','Rejected'):
            # Durable intent is saved with the decision; inference is dispatched
            # later by the existing single-worker scheduler, never in HTTP.
            db.db.execute('INSERT INTO WebReviewProductions(review_id,subject,created_at,updated_at) VALUES (?,?,?,?)',
                          (ident,entry['subject'],utcnow(),utcnow()))
        if row:
            if action=='Accepted':
                from .web_control import snapshot
                db.db.execute('INSERT INTO WebApprovals(job_id,revision,target,snapshot_json,created_at) VALUES (?,?,?,?,?)',
                              (entry['id'],row['revision'],'UserAccepted' if entry['acceptance_scope']=='delivery' else 'ModelAccepted',canonical(snapshot(db,entry['id'])),utcnow()))
                if entry['acceptance_scope']=='delivery':
                    db.db.execute("UPDATE WebControls SET state='UserAccepted',cursor=3,revision=revision+1,updated_at=? WHERE job_id=?",(utcnow(),entry['id']))
                else:
                    # Human approval is independent of technical completion.
                    # Keep the real cursor, failure, stage and production status.
                    db.db.execute('UPDATE WebControls SET revision=revision+1,updated_at=? WHERE job_id=?',(utcnow(),entry['id']))
            else:
                # Revoking a model acceptance does not cancel/restart its
                # production job or erase an earlier approval.
                state='Waiting' if row['state']=='UserAccepted' else row['state']
                cursor=2 if row['cursor']==3 else row['cursor']
                db.db.execute('UPDATE WebControls SET state=?,cursor=?,revision=revision+1,updated_at=? WHERE job_id=?',
                              (state,cursor,utcnow(),entry['id']))
            db.db.execute('INSERT INTO JobEvents(job_id,created_at,event,data_json) VALUES (?,?,?,?)',
                          (entry['id'],utcnow(),'WebModelReview',canonical({'review_id':ident,'action':action,'note':note,'model':entry['model']})))
        db.db.commit();return ident
    except BaseException:db.db.rollback();raise
