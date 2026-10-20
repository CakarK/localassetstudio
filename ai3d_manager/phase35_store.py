"""Phase35 sidecars isolate every new row from historical production databases."""
import json
from .common import AI,ProductionError,canonical,utcnow,intact
from pathlib import Path
from .database import Database
from .quality_store import QualityStore
from .controlled_generation import claim_budget


def open_state(root=None):
    root=root or AI
    db=Database(root/'State/phase35_jobs.sqlite',root/'Outputs/Phase35')
    store=QualityStore(root/'State/phase35_quality.sqlite',root/'Quality/Phase35')
    store.db.execute('CREATE TABLE IF NOT EXISTS Phase35Records(kind TEXT,request_key TEXT,report_json TEXT,created_at TEXT,PRIMARY KEY(kind,request_key))');store.db.commit()
    return db,store


def lookup(store,kind,key):
    row=store.db.execute('SELECT report_json FROM Phase35Records WHERE kind=? AND request_key=?',(kind,key)).fetchone()
    return json.loads(row[0]) if row else None


def record(store,kind,key,value):
    old=lookup(store,kind,key)
    if old is not None:return old
    store.db.execute('INSERT INTO Phase35Records VALUES (?,?,?,?)',(kind,key,canonical(value),utcnow()));store.db.commit();return value


def claim_inference(store,operation,attempt,asset_role,necessity_evidence=None):
    if operation not in ['reference','shape','pbr']:raise ValueError('Unknown phase35 inference operation')
    if asset_role!='small_accessory':raise ProductionError('Phase35BudgetDenied','Full building generation is forbidden; only one small accessory is allowed')
    if operation=='reference':
        if not intact(necessity_evidence or {}):raise ProductionError('ReferenceNecessityMissing','A hashed necessity report is required before the optional new reference')
        proof=json.loads(Path(necessity_evidence['path']).read_text('utf-8'))
        if proof.get('decision')!='ReferenceRequired' or not proof.get('existing_source_search') or not proof.get('reason'):raise ProductionError('ReferenceNecessityMissing','Existing sources and reason for a new reference must be documented')
    claim_budget(store,'phase35',operation,1,attempt)
