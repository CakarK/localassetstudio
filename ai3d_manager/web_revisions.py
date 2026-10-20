"""One bounded, persistent regeneration per human model review.

Old jobs/files are immutable. Existing composition, Worker and 3D queue do the
work; this adapter only records the user's delegated regeneration permission.
"""
import json
import sqlite3
from pathlib import Path
from .common import canonical,utcnow,intact
from .web_control import control,snapshot,authorize
from . import web_queue

TERMINAL=('Done','Blocked','Cancelled','Superseded')


def update(db,review_id,**values):
    values['updated_at']=utcnow()
    db.db.execute('UPDATE WebReviewProductions SET '+','.join(k+'=?' for k in values)+' WHERE review_id=?',
                  (*values.values(),review_id));db.db.commit()


def pending(db):
    return [dict(r) for r in db.db.execute("SELECT * FROM WebReviewProductions WHERE state NOT IN ('Done','Blocked','Cancelled','Superseded') ORDER BY review_id")]


def enroll(db):
    """Backfill only the latest negative decision; never replay old decisions."""
    db.db.execute('''INSERT OR IGNORE INTO WebReviewProductions(review_id,subject,created_at,updated_at)
      SELECT r.id,r.subject,?,? FROM WebModelReviews r
      WHERE r.action IN ('RevisionRequested','Rejected')
      AND r.id=(SELECT MAX(x.id) FROM WebModelReviews x WHERE x.subject=r.subject)''',(utcnow(),utcnow()))
    db.db.commit()


def current(db,task):
    row=db.db.execute('SELECT MAX(id) FROM WebModelReviews WHERE subject=?',(task['subject'],)).fetchone()
    return row[0]==task['review_id']


def reconcile(db):
    for task in pending(db):
        ident=task['child_job_id']
        if not current(db,task):
            # A superseded queued child must not later run via the manual queue.
            if ident:
                row=control(db,ident)
                if row['state']=='Running':continue  # finish owned in-flight work
                q=db.db.execute("SELECT id FROM WebProductionQueue WHERE job_id=? AND state='Queued'",(ident,)).fetchone()
                if q:web_queue.remove(db,q['id'])
            update(db,task['review_id'],state='Superseded');continue
        if not ident:continue
        row=control(db,ident)
        if not task['queue_id']:
            q=db.db.execute('SELECT id FROM WebProductionQueue WHERE job_id=? ORDER BY created_at DESC LIMIT 1',(ident,)).fetchone()
            if q:
                task['queue_id']=q['id'];update(db,task['review_id'],queue_id=q['id'])
        if row['state'] in ('Blocked','Rejected') or db.job(ident)['cancel_requested']:
            update(db,task['review_id'],state='Blocked' if row['state']=='Blocked' else 'Cancelled',error=row['error']);continue
        if row['cursor']>=2:
            update(db,task['review_id'],state='Done');continue
        if task['queue_id']:
            q=db.db.execute('SELECT state,error FROM WebProductionQueue WHERE id=?',(task['queue_id'],)).fetchone()
            if not q or q['state'] in ('Cancelled','Blocked'):
                update(db,task['review_id'],state='Cancelled' if not q or q['state']=='Cancelled' else 'Blocked',error=q['error'] if q else 'Kuyruk kaydı kaldırıldı.');continue
        if row['state']=='Running':
            state='Reference' if row['cursor']==0 else 'Production'
            if task['state']!=state:update(db,task['review_id'],state=state)


def source(db,task,ai_root):
    review=dict(db.db.execute('SELECT * FROM WebModelReviews WHERE id=?',(task['review_id'],)).fetchone())
    declared=json.loads(review['model_json']).get('info')
    if not declared or not intact(declared):raise ValueError('İncelenen kaynak model eksik veya değişmiş; revizyon başlatılmadı.')
    if task['subject'].startswith('web:'):
        job=db.job(task['subject'][4:])
        from .web_reviews import best_model
        model=best_model(snapshot(db,job['id']))
        if not model or model['info']!=declared:raise ValueError('Kaynak model inceleme kararından sonra değişmiş; yeni karar gerekli.')
    else:
        # Sprint databases are opened read-only. Regenerations live exclusively
        # in the web database and never mutate the old sprint or its manifest.
        manifest=json.loads((Path(ai_root)/'Library/manifest.json').read_text('utf-8'))
        item=manifest['assets'][task['subject'][8:]]
        path=Path(item['source_database']).resolve()
        allowed={ (Path(ai_root)/'State'/name).resolve() for name in ('asset_manager.sqlite','library_jobs.sqlite') }
        if path not in allowed:raise ValueError('Kütüphane kaynak veritabanı izin verilen konumda değil.')
        conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
        try:row=conn.execute('SELECT * FROM GenerationJobs WHERE id=?',(item['job_id'],)).fetchone();job=dict(row) if row else None
        finally:conn.close()
        if not job:raise ValueError('Kütüphane kaynak üretim kaydı bulunamadı.')
    return json.loads(job['request_json']),json.loads(job['settings_json']),review


def prepare(db,task,ai_root):
    """Freeze CPU composition before enqueue, and recover a crash after enqueue."""
    from .web_design import compose
    from .web_policy import ground_free_asset,GROUND_NEGATIVE
    from .worker import settings_for,ALLOWED_SETTINGS
    from .prompts import prepare as prepare_prompt
    from .web_control import QUALITY_PATH,QUALITY_ROOT
    variant='web-review-'+str(task['review_id'])
    prior=db.db.execute('SELECT * FROM GenerationJobs WHERE variant=? ORDER BY created_at LIMIT 1',(variant,)).fetchone()
    if prior:
        job=dict(prior)
    else:
        if task['prepared_json']:
            prepared=json.loads(task['prepared_json']);asset=prepared['asset'];settings=prepared['settings']
        else:
            asset,old_settings,review=source(db,task,ai_root)
            # Use the actual customized source request, not the original catalog
            # entry; otherwise revisions silently lose the user's earlier edits.
            base_prompt=old_settings.get('prompt') or asset.get('web_base_prompt') or asset['prompt']
            base_translation=None
            if old_settings.get('prompt_language',asset.get('prompt_language','en'))=='tr':
                base_translation=prepare_prompt(asset,{**old_settings,'prompt':base_prompt})
                base_prompt=base_translation['translated']
            asset={**asset,'prompt':base_prompt}
            composed=compose(asset,review['note'],review_feedback=True)
            if base_translation:composed['source_prompt_translation']=base_translation
            asset=ground_free_asset({**asset,'catalog_original_prompt':asset.get('catalog_original_prompt',asset['prompt']),
                'prompt':composed['prompt'],'prompt_language':'en','prompt_is_customized':True,
                'web_base_prompt':composed['prompt'],'web_customization':review['note'],'web_customization_language':'tr',
                'web_composition':composed,'web_design':{'notes':review['note']},
                'web_revision':{'review_id':review['id'],'parent_subject':task['subject'],'action':review['action'],
                                'note':review['note'],'source_model':json.loads(review['model_json']),
                                'authorization':'user_requested_automatic_review_regeneration'},
                'quality_sidecar':{'db':str(QUALITY_PATH),'root':str(QUALITY_ROOT)}})
            provider=old_settings.get('image_provider',asset.get('image_provider','z-image-turbo'))
            asset['image_provider']=provider
            settings=settings_for(asset,{**{k:v for k,v in old_settings.items() if k in ALLOWED_SETTINGS},
                'seed':(old_settings.get('seed',42)+review['id']*104729)%2147483648,
                'image_provider':provider,'prompt':composed['prompt'],'prompt_language':'en','style':asset['shared_style'],
                'negative':asset['shared_negative']+', '+GROUND_NEGATIVE,'max_transient_retries':0,
                'allow_quality_reduction':False,'shape_steps':40,'shape_resolution':384,'subprocess_timeout':600})
            update(db,task['review_id'],prepared_json=canonical({'asset':asset,'settings':settings}))
        # The Assets catalog row is not overwritten with a revision's prompt.
        db.db.execute('INSERT OR IGNORE INTO Assets VALUES (?,?,?)',(asset['asset_id'],canonical(asset),utcnow()));db.db.commit()
        job,_=db.enqueue(asset,variant,settings)
    db.db.execute('INSERT OR IGNORE INTO WebControls(job_id,updated_at) VALUES (?,?)',(job['id'],utcnow()));db.db.commit()
    latest=db.db.execute('SELECT state,error FROM WebReviewProductions WHERE review_id=?',(task['review_id'],)).fetchone()
    state=latest['state'] if latest['state'] in TERMINAL else 'Pending'
    update(db,task['review_id'],child_job_id=job['id'],state=state,error=latest['error'] if state in TERMINAL else None)
    if state in ('Cancelled','Superseded'):
        db.cancel(job['id'])
        db.db.execute("UPDATE WebControls SET state='Rejected',updated_at=? WHERE job_id=? AND state='Waiting'",(utcnow(),job['id']));db.db.commit()
    return job['id']


def advance(db,task,launch):
    """Called only when no web worker runs; queue/Worker keep their own locks."""
    ident=task['child_job_id'];row=control(db,ident)
    if row['state']!='Waiting':return
    if row['cursor']==0:
        target=authorize(db,ident,row['revision'])
        update(db,task['review_id'],state='Reference')
        launch(db,ident,target)
    elif row['cursor']==1:
        # This is delegated review authorization, never a claim that a human
        # personally inspected the newly generated reference or that AI passed it.
        qid=web_queue.add(db,[{'id':ident,'revision':row['revision']}])[0]
        update(db,task['review_id'],queue_id=qid,state='Queued')
        db.event(ident,'ReviewRegenerationSelected',{'review_id':task['review_id'],'parent_subject':task['subject'],
                    'authorization':'user_requested_automatic_review_regeneration','reference':snapshot(db,ident)['ReferenceGenerated']['reference']})
