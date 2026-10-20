"""Persistent 3D-only queue. Every entry is an explicit human reference selection."""
import json
import uuid
from .common import canonical,utcnow,intact
from .web_control import control,snapshot,reference_ready

def rows(db):
    return [dict(r) for r in db.db.execute('SELECT * FROM WebProductionQueue ORDER BY position,created_at,id')]

def add(db,selections):
    if not isinstance(selections,list) or not 1<=len(selections)<=100:raise ValueError('1–100 referans seçin.')
    if any(not isinstance(s,dict) or not isinstance(s.get('id'),str) or type(s.get('revision'))!=int for s in selections):raise ValueError('Geçersiz referans seçimi.')
    if len({s['id'] for s in selections})!=len(selections):raise ValueError('Aynı referans iki kez seçilemez.')
    db.db.execute('BEGIN IMMEDIATE');added=[]
    try:
        position=db.db.execute('SELECT COALESCE(MAX(position),0) FROM WebProductionQueue').fetchone()[0]
        for selected in selections:
            ident=selected['id'];row=control(db,ident)
            existing=db.db.execute("SELECT * FROM WebProductionQueue WHERE job_id=? AND state IN ('Queued','Running')",(ident,)).fetchone()
            if existing:
                # The same revision sent by two browser tabs is one selection, not two jobs.
                if existing['revision']!=selected['revision']+1:raise ValueError('Seçim güncel değil; ekranı yenileyin.')
                added.append(existing['id']);continue
            if row['state'] not in ('Waiting','Blocked') or not reference_ready(db,ident) or row['revision']!=selected['revision']:
                raise ValueError('Yalnız görseli hazır ve henüz üretilmemiş referanslar seçilebilir.')
            outputs=snapshot(db,ident);reference=outputs.get('ReferenceGenerated',{}).get('reference')
            if not reference or not intact(reference):raise ValueError('Seçilen referans eksik veya değiştirilmiş; 3D kuyruğuna alınmadı.')
            qid=uuid.uuid4().hex;now=utcnow();position+=1
            db.db.execute('INSERT INTO WebApprovals(job_id,revision,target,snapshot_json,created_at) VALUES (?,?,?,?,?)',
                (ident,row['revision'],'Production',canonical(outputs),now))
            db.db.execute("INSERT INTO WebProductionQueue VALUES (?,?,?,?,?,?,?,?,?,?)",
                (qid,ident,row['revision']+1,position,'Queued',canonical(outputs),now,None,None,None))
            db.db.execute("UPDATE WebControls SET state='Queued',cursor=1,revision=revision+1,error=NULL,updated_at=? WHERE job_id=?",(now,ident))
            added.append(qid)
        db.db.commit();return added
    except BaseException:db.db.rollback();raise

def settle(db):
    """A finished subprocess is never silently retried, including after server restart."""
    for item in rows(db):
        if item['state']!='Running':continue
        row=control(db,item['job_id'])
        if row['state']=='Running':continue
        status='Done' if row['cursor']>=2 and row['state'] in ('Waiting','UserAccepted') else 'Cancelled' if row['state']=='Rejected' else 'Blocked'
        db.db.execute('UPDATE WebProductionQueue SET state=?,ended_at=?,error=? WHERE id=?',
            (status,utcnow(),row['error'],item['id']))
    db.db.commit()

def claim_next(db,automatic_jobs=(),*,selected_job=None):
    db.db.execute('BEGIN IMMEDIATE')
    try:
        running=db.setting('web_queue_running',False)
        automatic=set(automatic_jobs) if not db.setting('web_revision_paused',False) else set()
        if (not running and not automatic) or db.db.execute("SELECT 1 FROM WebControls WHERE state='Running'").fetchone():
            db.db.commit();return None
        for item in rows(db):
            if item['state']!='Queued':continue
            if selected_job is not None and item['job_id']!=selected_job:continue
            # Review-authorized work may start itself, but must not implicitly
            # start ordinary references the user has left in a stopped queue.
            if not running and item['job_id'] not in automatic:continue
            row=control(db,item['job_id']);expected=json.loads(item['snapshot_json']);current=snapshot(db,item['job_id'])
            reference=current.get('ReferenceGenerated',{}).get('reference')
            if row['state']!='Queued' or row['revision']!=item['revision'] or current!=expected or not reference or not intact(reference):
                message='Seçilen referans veya onay kaydı değişmiş; otomatik üretim engellendi.'
                db.db.execute("UPDATE WebProductionQueue SET state='Blocked',ended_at=?,error=? WHERE id=?",(utcnow(),message,item['id']))
                db.db.execute("UPDATE WebControls SET state='Blocked',error=? WHERE job_id=? AND state='Queued'",(message,item['job_id']))
                continue
            db.db.execute("UPDATE WebControls SET state='Running',pid=NULL,error=NULL,updated_at=? WHERE job_id=?",(utcnow(),item['job_id']))
            db.db.execute("UPDATE WebProductionQueue SET state='Running',started_at=? WHERE id=?",(utcnow(),item['id']))
            db.db.commit();return item['job_id']
        # An empty/completed batch stops. Items added later need another explicit Start.
        if selected_job is None:db.db.execute("INSERT OR REPLACE INTO GenerationSettings VALUES ('web_queue_running','false')")
        db.db.commit();return None
    except BaseException:db.db.rollback();raise

def remove(db,qid):
    db.db.execute('BEGIN IMMEDIATE')
    try:
        item=db.db.execute('SELECT * FROM WebProductionQueue WHERE id=?',(qid,)).fetchone()
        if not item or item['state']!='Queued':raise ValueError('Yalnız bekleyen kuyruk öğesi çıkarılabilir.')
        db.db.execute("UPDATE WebProductionQueue SET state='Cancelled',ended_at=? WHERE id=?",(utcnow(),qid))
        db.db.execute("UPDATE WebControls SET state='Waiting',revision=revision+1,updated_at=? WHERE job_id=? AND state='Queued'",(utcnow(),item['job_id']))
        db.db.commit()
    except BaseException:db.db.rollback();raise

def reorder(db,ids):
    db.db.execute('BEGIN IMMEDIATE')
    try:
        waiting=[r['id'] for r in rows(db) if r['state']=='Queued']
        if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or len(ids)!=len(waiting) or set(ids)!=set(waiting):raise ValueError('Kuyruk değişti; güncel bekleyen öğelerin tamamını sıralayın.')
        for pos,ident in enumerate(ids):db.db.execute('UPDATE WebProductionQueue SET position=? WHERE id=?',(pos,ident))
        db.db.execute("DELETE FROM GenerationSettings WHERE key='web_production_order'")
        db.db.commit()
    except BaseException:db.db.rollback();raise
