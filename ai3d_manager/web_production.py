"""One visible dispatch order over the existing review and 3D queues."""
import json
import sqlite3
from pathlib import Path
from .common import canonical,utcnow
from .web_control import control,STEPS,LABELS
from . import web_queue,web_revisions

LABEL=dict(zip(STEPS,LABELS))|{'Preparing':'Revizyon notları ve prompt hazırlanıyor (CPU)',
    'GeometryIdentityGate':'Geometri kimlik kontrolü','FinalVisual':'Qwen son model değerlendirmesi',
    'Pending':'Referans hazırlığı bekliyor','Queued':'3D üretimi bekliyor'}


def candidates(db):
    """This exact list is consumed by both the dispatcher and the UI."""
    tasks=web_revisions.pending(db);by_child={t['child_job_id']:t for t in tasks if t['child_job_id']}
    result=[];seen=set();paused=db.setting('web_revision_paused',False)
    for q in web_queue.rows(db):
        if q['state']!='Queued':continue
        task=by_child.get(q['job_id'])
        if task:
            result.append({'key':'r:'+str(task['review_id']),'kind':'revision','job_id':q['job_id'],
                           'queue_id':q['id'],'task':task,'enabled':not paused});seen.add(task['review_id'])
        else:result.append({'key':'q:'+q['id'],'kind':'manual','job_id':q['job_id'],'queue_id':q['id'],
                            'enabled':db.setting('web_queue_running',False)})
    for task in tasks:
        if task['review_id'] in seen:continue
        if task['child_job_id'] and control(db,task['child_job_id'])['state']=='Running':continue
        result.append({'key':'r:'+str(task['review_id']),'kind':'revision','job_id':task['child_job_id'],
                       'queue_id':None,'task':task,'enabled':not paused})
    order={key:i for i,key in enumerate(db.setting('web_production_order',[]))}
    fallback={item['key']:i for i,item in enumerate(result)}
    return sorted(result,key=lambda item:(not item['enabled'],
        0 if item.get('task',{}).get('state')=='Preparing' else 1,
        order.get(item['key'],len(order)+fallback[item['key']])))


def reorder(db,keys):
    if not isinstance(keys,list) or any(not isinstance(k,str) for k in keys):raise ValueError('Geçerli üretim sırası gerekli.')
    db.db.execute('BEGIN IMMEDIATE')
    try:
        current=[i['key'] for i in candidates(db) if i.get('task',{}).get('state')!='Preparing']
        if len(keys)!=len(current) or set(keys)!=set(current):raise ValueError('Sıra değişti. Güncel listeyi yenileyin.')
        db.db.execute('INSERT OR REPLACE INTO GenerationSettings VALUES (?,?)',('web_production_order',canonical(keys)))
        db.db.commit()
    except BaseException:db.db.rollback();raise


def cancel(db,key):
    item=next((i for i in candidates(db) if i['key']==key),None)
    if not item:raise ValueError('Yalnız bekleyen üretim çıkarılabilir; listeyi yenileyin.')
    if item['kind']=='manual':web_queue.remove(db,item['queue_id']);return
    task=item['task'];ident=item['job_id']
    if ident:
        row=control(db,ident)
        if row['state']=='Running':raise ValueError('Çalışan model sıradan çıkarılamaz.')
        if item['queue_id']:web_queue.remove(db,item['queue_id'])
        db.cancel(ident)
        db.db.execute("UPDATE WebControls SET state='Rejected',revision=revision+1,updated_at=? WHERE job_id=?",(utcnow(),ident));db.db.commit()
    web_revisions.update(db,task['review_id'],state='Cancelled',error='Kullanıcı üretim sırasından çıkardı.')


def error_summary(value):
    if not value:return None
    try:
        parsed=json.loads(value)
        if isinstance(parsed,dict):return {'GeometryIdentityGateFailed':'Geometri nesne bütünlüğü kontrolünden geçemedi.',
            'GeometryIdentityGateReview':'Geometri insan incelemesi gerektiriyor.','GPUBusy':'GPU başka bir işlem tarafından kullanılıyor.'}.get(parsed.get('category'),parsed.get('message',value))
    except (ValueError,TypeError):pass
    return value


def board(db,quality_path):
    jobs={j['id']:j for j in db.jobs()};tasks=[dict(r) for r in db.db.execute('SELECT * FROM WebReviewProductions ORDER BY review_id')]
    by_child={t['child_job_id']:t for t in tasks if t['child_job_id']};job_assets={}
    for ident,j in jobs.items():job_assets[ident]=json.loads(j['request_json'])
    catalog=None
    quality=None
    if Path(quality_path).is_file():quality=sqlite3.connect(Path(quality_path).resolve().as_uri()+'?mode=ro',uri=True);quality.row_factory=sqlite3.Row
    def row(item,state):
        nonlocal catalog
        task=item.get('task');ident=item.get('job_id');job=jobs.get(ident);asset=job_assets.get(ident)
        if not asset and task:
            asset=job_assets.get(task['subject'][4:]) if task['subject'].startswith('web:') else None
            if not asset:
                if catalog is None:
                    from .catalog import load_catalog
                    catalog={a['asset_id']:a for a in load_catalog()}
                asset=catalog.get(task['subject'][8:],{'asset_id':task['subject'][8:],'name_tr':task['subject'][8:]})
        asset=asset or {};ctl=control(db,ident) if job else None;stages=db.stages(ident) if job else []
        completed={s['name'] for s in stages if s['status']=='Completed'}
        active=next((s for s in reversed(stages) if s['status']=='Running'),None)
        failed=next((s for s in reversed(stages) if s['status']=='Failed'),None)
        stage='Preparing' if task and task['state']=='Preparing' and not ident else 'Pending' if not ident or (ctl and ctl['cursor']==0) else 'Queued'
        started=active['started_at'] if active else task['updated_at'] if stage=='Preparing' and task else None
        if active:stage=active['name']
        elif failed and state=='Blocked':stage=failed['name']
        elif ctl and (ctl['state']=='Running' or state=='Blocked'):
            if 'Packaged' in completed:stage='FinalVisual'
            elif 'GeometryGenerated' in completed and 'GeometryOptimized' not in completed:stage='GeometryIdentityGate'
            else:stage=next((s for s in STEPS if s not in completed and s not in ('ReferenceFidelity','UserAccepted')),'FinalVisual')
        if stage=='FinalVisual' and quality:
            q=quality.execute('SELECT status,created_at FROM QualityEvaluations WHERE job_id=? ORDER BY created_at DESC LIMIT 1',(ident,)).fetchone()
            if q and q['status']=='Running':started=q['created_at']
        note=''
        if task:
            review=db.db.execute('SELECT note,created_at FROM WebModelReviews WHERE id=?',(task['review_id'],)).fetchone()
            if review:note=review['note']
        reference=next((s for s in reversed(stages) if s['name']=='ReferenceGenerated' and s['status']=='Completed'),None)
        model=any(s in completed for s in ('GeometryGenerated','GeometryOptimized','PBRGenerated','Scaled','Packaged'))
        error=None if state=='Done' else (ctl['error'] if ctl else None) or (task['error'] if task else item.get('error'))
        decision=db.db.execute('SELECT action,model_json FROM WebModelReviews WHERE subject=? ORDER BY id DESC LIMIT 1',('web:'+ident,)).fetchone() if ident else None
        accepted=False
        if decision and decision['action']=='Accepted':
            from .web_reviews import best_model
            current_model=best_model({s['name']:json.loads(s['outputs_json'] or '{}') for s in stages if s['status']=='Completed'})
            accepted=bool(current_model and current_model['info']['sha256']==json.loads(decision['model_json']).get('info',{}).get('sha256'))
        return {'key':item['key'],'kind':item['kind'],'job_id':ident,'name':asset.get('name_tr',asset.get('asset_id','Varlık')),
            'asset_id':asset.get('asset_id'),'state':state,'enabled':item.get('enabled',True),'note':note,
            'stage':stage,'stage_label':LABEL.get(stage,stage),'stage_started_at':started,
            'created_at':task['created_at'] if task else item.get('created_at',job['created_at'] if job else None),
            'updated_at':ctl['updated_at'] if ctl else task['updated_at'] if task else None,
            'completed_stages':len(completed)+(1 if ctl and ctl['cursor']>=2 else 0),'total_stages':13,'error':error_summary(error),'error_detail':error,
            'pbr_ready':'PBRGenerated' in completed,
            'thumbnail':f'/files/job/{ident}/ReferenceGenerated/reference' if reference else None,
            'review_url':'/reviews?job='+ident if model else None,'job_url':'/?job='+ident if ident else None,
            'can_remove':state=='Queued','can_reorder':state=='Queued',
            'review_state':decision['action'] if decision and (decision['action']!='Accepted' or accepted) else 'Pending',
            'accepted':bool((ctl and ctl['state']=='UserAccepted') or accepted)}
    try:
        waiting=[];active=[]
        for item in candidates(db):
            if item.get('task',{}).get('state')=='Preparing':active.append(row(item,'Preparing'))
            else:waiting.append(row(item,'Queued'))
        for ctl in db.db.execute("SELECT * FROM WebControls WHERE state='Running'"):
            task=by_child.get(ctl['job_id']);item={'key':'r:'+str(task['review_id']) if task else 'job:'+ctl['job_id'],
                'kind':'revision' if task else 'manual','job_id':ctl['job_id'],'task':task}
            active.append(row(item,'Running'))
        history=[]
        for task in tasks:
            if task['state'] in web_revisions.TERMINAL:
                history.append(row({'key':'r:'+str(task['review_id']),'kind':'revision','job_id':task['child_job_id'],'task':task},task['state']))
        latest={q['job_id']:q for q in web_queue.rows(db)}
        for ident,q in latest.items():
            if ident in by_child or q['state'] in ('Queued','Running'):continue
            ctl=control(db,ident)
            # Old failed attempts do not describe the current recovered model.
            state='Done' if ctl['cursor']>=2 and ctl['state'] in ('Waiting','UserAccepted') else q['state']
            history.append(row({'key':'q:'+q['id'],'kind':'manual','job_id':ident,**q},state))
        history.sort(key=lambda i:i['updated_at'] or i['created_at'] or '',reverse=True)
        return {'server_time':utcnow(),'active':active,'waiting':waiting,'history':history,
            'running':bool(active or any(i['enabled'] for i in waiting)),
            'paused':db.setting('web_revision_paused',False) and not db.setting('web_queue_running',False),
            'counts':{'active':len(active),'waiting':len(waiting),'ready':sum(i['state']=='Done' and not i['accepted'] and i['review_state']=='Pending' for i in history),
                      'blocked':sum(i['state']=='Blocked' for i in history)}}
    finally:
        if quality:quality.close()
