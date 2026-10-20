import json
import sqlite3
import uuid
from pathlib import Path
from .common import AI, atomic_json, canonical, digest, safe_name, utcnow

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS Assets(asset_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS AssetVariants(asset_id TEXT, variant TEXT, last_version INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(asset_id,variant), FOREIGN KEY(asset_id) REFERENCES Assets(asset_id));
CREATE TABLE IF NOT EXISTS GenerationJobs(id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, variant TEXT NOT NULL,
 version INTEGER NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, request_json TEXT NOT NULL,
 settings_json TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, output_dir TEXT NOT NULL,
 cancel_requested INTEGER NOT NULL DEFAULT 0, error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS JobStages(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, name TEXT NOT NULL,
 fingerprint TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
 parameters_json TEXT NOT NULL, inputs_json TEXT NOT NULL, outputs_json TEXT, error_json TEXT,
 FOREIGN KEY(job_id) REFERENCES GenerationJobs(id));
CREATE TABLE IF NOT EXISTS JobAttempts(id TEXT PRIMARY KEY, stage_id TEXT UNIQUE NOT NULL, job_id TEXT NOT NULL,
 stage_name TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL, client_token TEXT UNIQUE NOT NULL,
 prompt_id TEXT, process_id INTEGER, work_dir TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
 started_at TEXT NOT NULL, ended_at TEXT, error_json TEXT,
 FOREIGN KEY(stage_id) REFERENCES JobStages(id));
CREATE TABLE IF NOT EXISTS JobArtifacts(id INTEGER PRIMARY KEY, stage_id TEXT NOT NULL, name TEXT NOT NULL,
 path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, UNIQUE(stage_id,name),
 FOREIGN KEY(stage_id) REFERENCES JobStages(id));
CREATE TABLE IF NOT EXISTS GenerationSettings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS JobEvents(id INTEGER PRIMARY KEY,job_id TEXT,created_at TEXT NOT NULL,event TEXT NOT NULL,data_json TEXT NOT NULL);
'''

class Database:
    def __init__(self,path=None,output_root=None):
        self.path=Path(path or AI/'State/asset_manager.sqlite').resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.output_root=Path(output_root or AI/'Outputs/Managed').resolve()
        self.db=sqlite3.connect(self.path,timeout=30)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO GenerationSettings VALUES ('queue_paused','false')");self.db.commit()

    def close(self):self.db.close()
    def event(self,job,event,data=None):
        self.db.execute('INSERT INTO JobEvents(job_id,created_at,event,data_json) VALUES (?,?,?,?)',(job,utcnow(),event,canonical(data or {})))
        self.db.commit()
    def setting(self,key,default=None):
        row=self.db.execute('SELECT value_json FROM GenerationSettings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO GenerationSettings VALUES (?,?)',(key,canonical(value)));self.db.commit()
    def sync_assets(self,assets):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO Assets VALUES (?,?,?)',[(a['asset_id'],canonical(a),utcnow()) for a in assets])
    def assets(self):
        rows=self.db.execute('SELECT * FROM Assets ORDER BY asset_id').fetchall()
        result=[]
        for row in rows:
            a=json.loads(row['data_json'])
            jobs=self.db.execute('SELECT id,variant,version,status FROM GenerationJobs WHERE asset_id=? ORDER BY created_at DESC',(a['asset_id'],)).fetchall()
            a['jobs']=[dict(j) for j in jobs];a['status']=jobs[0]['status'] if jobs else 'NotGenerated';result.append(a)
        return result
    def asset(self,asset_id):
        for asset in self.assets():
            if asset['asset_id']==asset_id:return asset
        raise ValueError('Unknown asset ID: '+asset_id)

    def enqueue(self,asset,variant,settings,new_version=False,priority=0):
        safe_name(asset['asset_id']);safe_name(variant)
        request={k:v for k,v in asset.items() if k not in ('jobs','status')}
        key=digest({'asset':request,'variant':variant,'settings':settings})
        self.db.execute('BEGIN IMMEDIATE')
        try:
            prior=self.db.execute('SELECT * FROM GenerationJobs WHERE idempotency_key=?',(key,)).fetchone()
            if prior and not new_version:self.db.commit();return dict(prior),False
            if new_version:key=digest({'request_key':key,'explicit_new_version':uuid.uuid4().hex})
            self.db.execute('INSERT OR IGNORE INTO AssetVariants VALUES (?,?,0)',(asset['asset_id'],variant))
            version=self.db.execute('SELECT last_version FROM AssetVariants WHERE asset_id=? AND variant=?',(asset['asset_id'],variant)).fetchone()[0]+1
            self.db.execute('UPDATE AssetVariants SET last_version=? WHERE asset_id=? AND variant=?',(version,asset['asset_id'],variant))
            ident=uuid.uuid4().hex
            out=self.output_root/asset['asset_id']/variant/f'v{version:04d}_{ident[:8]}'
            now=utcnow()
            self.db.execute('''INSERT INTO GenerationJobs(id,asset_id,variant,version,status,priority,request_json,settings_json,
                 idempotency_key,output_dir,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                 (ident,asset['asset_id'],variant,version,'Pending',priority,canonical(request),canonical(settings),key,str(out),now,now))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        out.mkdir(parents=True,exist_ok=False)
        atomic_json(out/'request.json',request)
        self.event(ident,'Enqueued',{'version':version,'idempotency_key':key})
        self.snapshot(ident)
        return self.job(ident),True

    def job(self,ident):
        row=self.db.execute('SELECT * FROM GenerationJobs WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Unknown job: '+ident)
        return dict(row)
    def jobs(self):return [dict(r) for r in self.db.execute('SELECT * FROM GenerationJobs ORDER BY priority DESC,created_at,id')]
    def next_job(self):
        row=self.db.execute("SELECT * FROM GenerationJobs WHERE status NOT IN ('Completed','Failed','Cancelled','NeedsReview','ReferenceReady') ORDER BY priority DESC,created_at,id LIMIT 1").fetchone()
        return dict(row) if row else None
    def status(self,ident,status,error=None):
        self.db.execute('UPDATE GenerationJobs SET status=?,error_json=?,updated_at=? WHERE id=?',
                        (status,canonical(error) if error else None,utcnow(),ident));self.db.commit()
        self.event(ident,status,error);self.snapshot(ident)
    def cancel(self,ident):
        job=self.job(ident)
        if job['status'] in ('Completed','Cancelled'):return
        self.db.execute('UPDATE GenerationJobs SET cancel_requested=1,updated_at=? WHERE id=?',(utcnow(),ident));self.db.commit()
        self.event(ident,'CancelRequested',{'policy':'finish owned in-flight stage; never interrupt foreign ComfyUI work'})
        if job['status'] in ('Pending','Paused','Failed','NeedsReview'):self.status(ident,'Cancelled')
    def resume(self,ident,settings=None):
        job=self.job(ident)
        current=json.loads(job['settings_json'])
        if settings:current.update(settings)
        self.db.execute('UPDATE GenerationJobs SET cancel_requested=0,settings_json=? WHERE id=?',(canonical(current),ident));self.db.commit()
        self.event(ident,'ResumeRequested',{'settings':current});self.status(ident,'Pending')
    def stages(self,ident):return [dict(r) for r in self.db.execute('SELECT * FROM JobStages WHERE job_id=? ORDER BY started_at,id',(ident,))]
    def attempts(self,ident):return [dict(r) for r in self.db.execute('SELECT * FROM JobAttempts WHERE job_id=? ORDER BY started_at,id',(ident,))]
    def artifact_dict(self,stage_id):
        return {r['name']:{'path':r['path'],'size':r['size'],'sha256':r['sha256']} for r in self.db.execute('SELECT * FROM JobArtifacts WHERE stage_id=?',(stage_id,))}
    def begin_stage(self,ident,name,fingerprint,params,inputs):
        job=self.job(ident);sid=uuid.uuid4().hex
        number=self.db.execute('SELECT COUNT(*) FROM JobAttempts WHERE job_id=? AND stage_name=?',(ident,name)).fetchone()[0]+1
        work=Path(job['output_dir'])/'attempts'/name/f'{number:03d}_{sid[:8]}'
        work.mkdir(parents=True,exist_ok=False);now=utcnow()
        with self.db:
            self.db.execute('INSERT INTO JobStages VALUES (?,?,?,?,?,?,?,?,?,?,?)',(sid,ident,name,fingerprint,'Running',now,None,canonical(params),canonical(inputs),None,None))
            self.db.execute('''INSERT INTO JobAttempts(id,stage_id,job_id,stage_name,number,state,client_token,work_dir,started_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',(sid,sid,ident,name,number,'Running','g3dc_'+sid,str(work),now))
        self.event(ident,'StageStarted',{'name':name,'attempt':number,'fingerprint':fingerprint})
        return dict(self.db.execute('SELECT * FROM JobAttempts WHERE id=?',(sid,)).fetchone())
    def attempt_update(self,attempt,**fields):
        allowed={'state','prompt_id','context_json','process_id','error_json','ended_at'}
        if not set(fields)<=allowed:raise ValueError('Unexpected attempt fields')
        self.db.execute('UPDATE JobAttempts SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),attempt));self.db.commit()
    def complete_stage(self,attempt,artifacts):
        with self.db:
            for name,info in artifacts.items():
                self.db.execute('INSERT INTO JobArtifacts(stage_id,name,path,size,sha256) VALUES (?,?,?,?,?)',(attempt['stage_id'],name,info['path'],info['size'],info['sha256']))
            now=utcnow()
            self.db.execute("UPDATE JobStages SET status='Completed',ended_at=?,outputs_json=? WHERE id=?",(now,canonical(artifacts),attempt['stage_id']))
            self.db.execute("UPDATE JobAttempts SET state='Completed',ended_at=? WHERE id=?",(now,attempt['id']))
        self.event(attempt['job_id'],'StageCompleted',{'name':attempt['stage_name'],'artifacts':artifacts})
    def fail_stage(self,attempt,error,review=False):
        state='NeedsReview' if review else 'Failed';payload=canonical(error);now=utcnow()
        with self.db:
            self.db.execute('UPDATE JobStages SET status=?,ended_at=?,error_json=? WHERE id=?',(state,now,payload,attempt['stage_id']))
            self.db.execute('UPDATE JobAttempts SET state=?,ended_at=?,error_json=? WHERE id=?',(state,now,payload,attempt['id']))
        self.event(attempt['job_id'],'Stage'+state,{'stage':attempt['stage_name'],**error})
    def snapshot(self,ident):
        job=self.job(ident)
        events=[dict(r) for r in self.db.execute('SELECT * FROM JobEvents WHERE job_id=? ORDER BY id',(ident,))]
        atomic_json(Path(job['output_dir'])/'job.json',{'job':job,'stages':self.stages(ident),'attempts':self.attempts(ident),'events':events})
