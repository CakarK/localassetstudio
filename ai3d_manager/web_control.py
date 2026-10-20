"""Human-paced adapter over the existing Worker, with separate web-only persistence."""
import json
from pathlib import Path
from .common import AI, Halt, ProductionError, canonical, utcnow, intact
from .database import Database
from .worker import Worker, STAGES

DB_PATH = AI/'State/web_jobs.sqlite'
OUTPUT_ROOT = AI/'Outputs/Web'
QUALITY_PATH = AI/'State/web_quality.sqlite'
QUALITY_ROOT = AI/'Quality/Web'
STEPS = list(STAGES)
STEPS.insert(STEPS.index('GeometryGenerated'), 'ReferenceFidelity')
STEPS += ['FinalVisual', 'UserAccepted']
LABELS = ['Prompt hazırlığı','Referans görsel','Arka plan temizliği','Referans dosya kontrolü',
          'Prompt uyumu','3D geometri','Poligon optimizasyonu','PBR kaplamalar','Ölçek ve pivot',
          'Blender ve LOD export','Önizlemeler','Teknik doğrulama','Teslim paketi','Qwen görsel değerlendirme','Son insan onayı']
PRODUCTION_STEPS=[s for s in STEPS[2:-1] if s!='ReferenceFidelity']

def reference_ready(db,ident):
    """A human may choose an intact reference even after an old AI rejection."""
    row=control(db,ident)
    eligible=(row['cursor']==1 and row['state'] in ('Waiting','Queued')) or (row['cursor']==0 and row['state'] in ('Waiting','Blocked'))
    if not eligible or db.job(ident)['cancel_requested']:return False
    if row['cursor']==0 and any(s['name']=='GeometryGenerated' for s in db.stages(ident)):return False
    ref=snapshot(db,ident).get('ReferenceGenerated',{}).get('reference')
    return bool(ref and intact(ref))

def human_reference_approval(db,ident):
    """Bind human reference approval to the selected file, revision and active run."""
    row=control(db,ident)
    approval=db.db.execute("SELECT * FROM WebApprovals WHERE job_id=? AND revision=? AND target='Production' ORDER BY id DESC LIMIT 1",(ident,row['revision']-1)).fetchone()
    ref=snapshot(db,ident).get('ReferenceGenerated',{}).get('reference')
    expected=json.loads(approval['snapshot_json']).get('ReferenceGenerated',{}).get('reference') if approval else None
    if row['state']!='Running' or row['cursor']!=1 or not ref or ref!=expected or not intact(ref):
        raise ProductionError('ApprovalInvalidated','Seçilen görsel için güncel insan onayı bulunamadı veya dosya değişti.')
    delegation=db.db.execute('SELECT review_id FROM WebReviewProductions WHERE child_job_id=?',(ident,)).fetchone()
    return {'approval_id':approval['id'],'revision':approval['revision'],'reference':ref,
            'policy':'review_regeneration_delegation' if delegation else 'human_reference_only',
            **({'review_id':delegation['review_id']} if delegation else {})}

def connect(path=DB_PATH, output_root=OUTPUT_ROOT):
    db = Database(path, output_root)
    db.db.executescript('''
    CREATE TABLE IF NOT EXISTS WebControls(job_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
      revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'Waiting', pid INTEGER,
      error TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS WebApprovals(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
      revision INTEGER NOT NULL, target TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS web_one_running ON WebControls(state) WHERE state='Running';
    CREATE TABLE IF NOT EXISTS WebProductionQueue(id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
      revision INTEGER NOT NULL, position INTEGER NOT NULL, state TEXT NOT NULL,
      snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, error TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS web_queue_job_active ON WebProductionQueue(job_id)
      WHERE state IN ('Queued','Running');
    CREATE TABLE IF NOT EXISTS WebModelReviews(id INTEGER PRIMARY KEY,subject TEXT NOT NULL,
      action TEXT NOT NULL,note TEXT NOT NULL,model_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS web_model_reviews_subject ON WebModelReviews(subject,id);
    CREATE TABLE IF NOT EXISTS WebReviewProductions(review_id INTEGER PRIMARY KEY,subject TEXT NOT NULL,
      child_job_id TEXT UNIQUE,state TEXT NOT NULL DEFAULT 'Pending',prepared_json TEXT,
      queue_id TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      FOREIGN KEY(review_id) REFERENCES WebModelReviews(id));
    ''')
    return db

def control(db, ident):
    row = db.db.execute('SELECT * FROM WebControls WHERE job_id=?',(ident,)).fetchone()
    if not row: raise ValueError('Bu iş web arayüzüne ait değil.')
    return dict(row)

def snapshot(db, ident):
    return {r['name']:db.artifact_dict(r['id']) for r in db.stages(ident) if r['status']=='Completed'}

def authorize(db, ident, revision):
    """Atomically approve one exact next step; concurrent tabs cannot dispatch twice."""
    db.db.execute('BEGIN IMMEDIATE')
    try:
        row=control(db,ident)
        if row['revision']!=revision or row['state']!='Waiting':
            raise ValueError('Ekran güncel değil veya iş inceleme/çalışma durumunda. Yenileyin.')
        if row['cursor']>=3: raise ValueError('İş zaten onaylandı.')
        if row['cursor']<2 and db.db.execute("SELECT 1 FROM WebControls WHERE state='Running'").fetchone():
            raise ValueError('Başka bir aşama çalışıyor; bitmesini bekleyin.')
        target=['ReferencePreview','Production','UserAccepted'][row['cursor']]
        db.db.execute('INSERT INTO WebApprovals(job_id,revision,target,snapshot_json,created_at) VALUES (?,?,?,?,?)',
            (ident,revision,target,canonical(snapshot(db,ident)),utcnow()))
        if target=='UserAccepted':
            from .web_reviews import append_review,best_model
            model=best_model(snapshot(db,ident))
            if model and not intact(model['info']):raise ValueError('Model dosyası değişmiş; onay kaydedilmedi.')
            append_review(db,'web:'+ident,'Accepted','',model)
            db.db.execute("UPDATE WebControls SET state='UserAccepted',cursor=cursor+1,revision=revision+1,updated_at=? WHERE job_id=?",(utcnow(),ident))
        else:
            db.db.execute("UPDATE WebControls SET state='Running',revision=revision+1,error=NULL,pid=NULL,updated_at=? WHERE job_id=?",(utcnow(),ident))
        db.db.commit()
        return target
    except BaseException:
        db.db.rollback();raise

def web_gate_profile(asset):
    from .gate_analysis import profile_for
    return profile_for(asset)


def gate_factory(db,store):
    from .quality_gates import QualityGateService
    from .image_providers import DualComfyQualityLease
    return QualityGateService(db,store,lease_factory=DualComfyQualityLease,profile_resolver=web_gate_profile)

class InteractiveWorker(Worker):
    def __init__(self,db,target):
        super().__init__(db,allow_gpu=True,gate_service_factory=gate_factory)
        self.target=target

    def stage_parameters(self,stage,asset,settings):
        params=super().stage_parameters(stage,asset,settings)
        if stage=='PromptPrepared' and asset.get('ground_free_prompt_version',0)>=2:
            from .common import file_info
            from . import web_policy
            params['web_ground_policy']={'version':asset['ground_free_prompt_version'],
                                        'code':file_info(Path(web_policy.__file__))['sha256']}
        return params

    def before_stage(self,stage,ident):
        if STAGES.index(stage)>STAGES.index(self.target):
            self.db.status(ident,'Paused');raise Halt('Bir sonraki aşama için insan onayı bekleniyor.')
        if stage in ('ReferenceApproved','GeometryGenerated'):
            approval=human_reference_approval(self.db,ident)
            self.db.event(ident,'WebHumanReferenceApprovalUsed',{'stage':stage,**approval})
            return  # The user, rather than a VLM, decides reference suitability.
        super().before_stage(stage,ident)

    def execute_stage(self,stage,attempt,asset,settings):
        # A changed/deleted earlier output must never trigger unapproved regeneration.
        if stage!=self.target:
            raise ProductionError('ApprovalInvalidated','Önceki çıktı değişmiş; yeniden üretim için yeni insan kararı gerekli: '+stage)
        if stage=='PromptPrepared':
            from .common import atomic_json
            result=super().execute_stage(stage,attempt,asset,settings)
            if asset.get('ground_free_prompt_version',0)>=2:
                from .web_policy import reference_prompt
                path=Path(result['prompt'])
                atomic_json(path,reference_prompt(json.loads(path.read_text('utf-8')),asset,settings))
            if asset.get('web_composition'):
                path=Path(attempt['work_dir'])/'design-composition.json'
                atomic_json(path,asset['web_composition'])
                result['design_composition']=path
            return result
        if stage=='ReferenceApproved':
            from .common import atomic_json
            approval=human_reference_approval(self.db,attempt['job_id'])
            result=super().execute_stage(stage,attempt,asset,{**settings,'reference_approved':True})
            path=Path(result['approval']);value=json.loads(path.read_text('utf-8'))
            atomic_json(path,{**value,'semantic_visual_approval':
                'human_delegated_regeneration' if approval['policy']=='review_regeneration_delegation' else 'human',
                'web_human_approval':approval})
            return result
        if stage=='GeometryOptimized':
            try:return super().execute_stage(stage,attempt,asset,settings)
            except ProductionError as exc:
                if exc.category!='ExportFailure' or 'Optimization did not satisfy triangle budget' not in str(exc):raise
                import sys
                from .processes import run_process
                work=Path(attempt['work_dir'])
                self.db.event(attempt['job_id'],'WebOptimizationRecovery',{'reason':str(exc),'target':settings['polygon_target']})
                run_process([sys.executable,'-m','ai3d_manager.web_mesh_recovery',self.path('GeometryGenerated','geometry'),work,
                             str(settings['polygon_target'])],work,settings['subprocess_timeout'],self.db,attempt,'optimize-recovery')
                return {'optimized':work/'optimized.glb','optimization_report':work/'optimization.json'}
        return super().execute_stage(stage,attempt,asset,settings)

    def gpu_stage(self,stage,attempt,settings):
        if stage!='ReferenceGenerated' and attempt['state'] not in ('Submitting','Submitted','SubmissionUnknown','NeedsReview'):
            from .image_providers import gpu_preflight
            gpu_preflight(settings['comfy_url'],12000)
        return super().gpu_stage(stage,attempt,settings)

def run_pipeline_step(db,ident,target):
    from .common import ProcessLock
    from .quality_store import QualityStore
    from .image_providers import DualComfyQualityLease
    if target=='ReferenceFidelity':
        with ProcessLock(db.path.with_suffix('.worker.lock')),ProcessLock(AI/'State/production.lock') as lock:
            store=QualityStore(QUALITY_PATH,QUALITY_ROOT)
            try:
                from .reference_fidelity import ReferenceFidelityEvaluator
                report=ReferenceFidelityEvaluator(db,store).evaluate(ident,parent_lock=lock)
                from .web_policy import require_ground_free
                require_ground_free(report)
                if report['status']!='PASS':raise ValueError('Prompt uyumu '+report['status']+'; Shape engellendi.')
            finally:store.close()
    elif target=='FinalVisual':
        from .quality_service import QualityService
        store=QualityStore(QUALITY_PATH,QUALITY_ROOT)
        try:
            report=QualityService(db,store,lease_factory=DualComfyQualityLease,fallback_invalid_extraction=True).evaluate(ident)
            if not report.get('scores'):raise ValueError('Qwen raporu tamamlanamadı; kalite kayıtlarını inceleyin.')
            from .web_policy import require_ground_free
            require_ground_free(report)
        finally:store.close()
    else:
        result=InteractiveWorker(db,target).run(ident)
        completed=[s for s in db.stages(ident) if s['name']==target and s['status']=='Completed']
        if not completed or result['status'] in ('Failed','NeedsReview','Cancelled'):
            raise ValueError(result.get('error_json') or result['status'])

def run_step(ident,target):
    db=connect()
    try:
        row=control(db,ident)
        if row['state']!='Running' or ['ReferencePreview','Production'][row['cursor']]!=target: raise ValueError('Geçersiz aşama izni.')
        approval=db.db.execute('SELECT * FROM WebApprovals WHERE job_id=? AND revision=?',(ident,row['revision']-1)).fetchone()
        if not approval or approval['target']!=target or json.loads(approval['snapshot_json'])!=snapshot(db,ident):
            raise ValueError('Onaylanan çıktı kaydı değişti; işlem durduruldu.')
        # Keep web_policy.py frozen: its hash belongs to existing prompt checkpoints.
        # Reference previews now stop immediately for the user's visual decision.
        targets=['PromptPrepared','ReferenceGenerated'] if target=='ReferencePreview' else PRODUCTION_STEPS
        for stage in targets:
            if db.job(ident)['cancel_requested']:raise ValueError('İş kullanıcı tarafından durduruldu.')
            run_pipeline_step(db,ident,stage)
        if db.job(ident)['cancel_requested']:raise ValueError('İş kullanıcı tarafından durduruldu; tamamlanan çıktılar korundu.')
        db.db.execute("UPDATE WebControls SET cursor=cursor+1,state='Waiting',pid=NULL,updated_at=? WHERE job_id=?",(utcnow(),ident));db.db.commit()
    except Exception as exc:
        db.db.execute("UPDATE WebControls SET state='Blocked',error=?,pid=NULL,updated_at=? WHERE job_id=?",(str(exc),utcnow(),ident));db.db.commit()
        raise
    finally:db.close()

if __name__=='__main__':
    import sys
    run_step(sys.argv[1],sys.argv[2])
