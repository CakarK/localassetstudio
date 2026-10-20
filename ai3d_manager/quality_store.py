"""Sidecar quality persistence; never mutates production job/stage status."""
import json
import sqlite3
import uuid
from pathlib import Path
from .common import AI, atomic_json, canonical, utcnow

class QualityStore:
    def __init__(self,path=None,output_root=None):
        self.path=Path(path or AI/'State/quality.sqlite').resolve();self.path.parent.mkdir(parents=True,exist_ok=True)
        self.output_root=Path(output_root or AI/'Quality').resolve()
        self.db=sqlite3.connect(self.path,timeout=30);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS QualityEvaluations(id TEXT PRIMARY KEY,job_id TEXT,asset_id TEXT,status TEXT,created_at TEXT,ended_at TEXT,work_dir TEXT,config_json TEXT,report_json TEXT,error_json TEXT);
        CREATE TABLE IF NOT EXISTS QualityActions(id INTEGER PRIMARY KEY,evaluation_id TEXT,job_id TEXT,action TEXT,reason TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS QualityPlans(id TEXT PRIMARY KEY,evaluation_id TEXT,created_at TEXT,plan_json TEXT);
        CREATE TABLE IF NOT EXISTS QualityCandidates(id TEXT PRIMARY KEY,parent_job_id TEXT,plan_id TEXT,candidate_number INTEGER,job_id TEXT,status TEXT,request_json TEXT,created_at TEXT,UNIQUE(plan_id,candidate_number));
        CREATE TABLE IF NOT EXISTS QualityBenchmarks(id TEXT PRIMARY KEY,asset_id TEXT,model TEXT,status TEXT,report_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS QualityRefinements(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,job_id TEXT,report_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS QualityImprovements(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,parent_job_id TEXT,status TEXT,config_json TEXT,plan_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS QualityGateReports(id TEXT PRIMARY KEY,job_id TEXT,kind TEXT,status TEXT,request_key TEXT,created_at TEXT,work_dir TEXT,report_json TEXT);
        CREATE TABLE IF NOT EXISTS ReferenceSets(id TEXT PRIMARY KEY,asset_id TEXT UNIQUE,request_key TEXT UNIQUE,status TEXT,created_at TEXT,plan_json TEXT);
        CREATE TABLE IF NOT EXISTS ReferenceCandidates(id TEXT PRIMARY KEY,set_id TEXT,slot INTEGER,seed INTEGER,job_id TEXT,gate_report_id TEXT,metadata_json TEXT,UNIQUE(set_id,slot));
        CREATE TABLE IF NOT EXISTS ReferenceSelections(id TEXT PRIMARY KEY,set_id TEXT,selection_key TEXT UNIQUE,created_at TEXT,report_json TEXT);
        CREATE TABLE IF NOT EXISTS ControlledGenerations(id TEXT PRIMARY KEY,set_id TEXT UNIQUE,job_id TEXT,evaluation_id TEXT,status TEXT,metadata_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS ProductionBudgetClaims(scope_id TEXT,operation TEXT,slot INTEGER,owner_attempt TEXT,created_at TEXT,PRIMARY KEY(scope_id,operation,slot));
        CREATE TABLE IF NOT EXISTS ImageBenchmarks(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,config_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS ImageReferences(id TEXT PRIMARY KEY,benchmark_id TEXT,asset_id TEXT,provider_id TEXT,profile TEXT,seed INTEGER,job_id TEXT,gate_report_id TEXT,fidelity_report_id TEXT,metadata_json TEXT,UNIQUE(benchmark_id,asset_id,provider_id,profile,seed));
        CREATE TABLE IF NOT EXISTS ReferenceFidelityReports(id TEXT PRIMARY KEY,job_id TEXT,request_key TEXT,status TEXT,work_dir TEXT,report_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS ImageShapeRuns(id TEXT PRIMARY KEY,benchmark_id TEXT,reference_id TEXT UNIQUE,job_id TEXT,evaluation_id TEXT,status TEXT,metadata_json TEXT);
        ''');self.db.commit()
    def close(self):self.db.close()
    def begin(self,job,config,ident=None):
        ident=ident or uuid.uuid4().hex
        previous=self.db.execute('SELECT * FROM QualityEvaluations WHERE id=?',(ident,)).fetchone()
        if previous:
            if previous['job_id']!=job['id'] or previous['config_json']!=canonical(config):raise ValueError('Evaluation reservation belongs to another request')
            return dict(previous)
        work=self.output_root/job['id']/ident;work.mkdir(parents=True,exist_ok=False)
        self.db.execute('INSERT INTO QualityEvaluations VALUES (?,?,?,?,?,?,?,?,?,?)',(ident,job['id'],job['asset_id'],'Running',utcnow(),None,str(work),canonical(config),None,None));self.db.commit()
        return self.get(ident)
    def get(self,ident):
        row=self.db.execute('SELECT * FROM QualityEvaluations WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Unknown quality evaluation: '+ident)
        return dict(row)
    def latest(self,job_id):
        row=self.db.execute('SELECT * FROM QualityEvaluations WHERE job_id=? ORDER BY created_at DESC LIMIT 1',(job_id,)).fetchone()
        return dict(row) if row else None
    def finish(self,ident,report):
        work=Path(self.get(ident)['work_dir']);atomic_json(work/'visual-validation.json',report)
        self.db.execute('UPDATE QualityEvaluations SET status=?,ended_at=?,report_json=? WHERE id=?',(report['decision'],utcnow(),canonical(report),ident));self.db.commit()
    def fail(self,ident,error):
        self.db.execute("UPDATE QualityEvaluations SET status='NeedsReview',ended_at=?,error_json=? WHERE id=?",(utcnow(),canonical(error),ident));self.db.commit()
    def report(self,job):
        row=self.latest(job)
        if not row:raise ValueError('No visual evaluation for '+job)
        return json.loads(row['report_json']) if row['report_json'] else {'evaluation_id':row['id'],'job_id':job,'decision':'NeedsReview','scores':None,'overall_score':None,'error':json.loads(row['error_json'] or '{}')}
    def action(self,job,action,reason):
        if not reason or not reason.strip():raise ValueError('An explicit audit reason is required')
        row=self.latest(job)
        if not row:raise ValueError('Evaluate this job before manual review')
        self.db.execute('INSERT INTO QualityActions(evaluation_id,job_id,action,reason,created_at) VALUES (?,?,?,?,?)',(row['id'],job,action,reason.strip(),utcnow()));self.db.commit()
        return dict(self.db.execute('SELECT * FROM QualityActions ORDER BY id DESC LIMIT 1').fetchone())
    def actions(self,job):return [dict(x) for x in self.db.execute('SELECT * FROM QualityActions WHERE job_id=? ORDER BY id',(job,))]
    def evaluations(self,asset):return [dict(x) for x in self.db.execute('SELECT * FROM QualityEvaluations WHERE asset_id=? ORDER BY created_at',(asset,))]
    def gate_report(self,job,kind):
        row=self.db.execute('SELECT report_json FROM QualityGateReports WHERE job_id=? AND kind=? ORDER BY created_at DESC LIMIT 1',(job,kind)).fetchone()
        return json.loads(row[0]) if row else None
