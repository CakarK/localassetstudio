"""Source-anchored PNG requirements, separate from object identity gates."""
import json
import re
import time
import uuid
from pathlib import Path
from .common import AI, ProductionError, atomic_json, canonical, digest, file_info, intact, utcnow
from .quality_requirements import requirements, criterion, classify
from .quality_schema import obj, enum
from .quality_vlm import OllamaVLM, GPUQualityLease
from .quality_gates import outputs_for
from .image_providers import DualComfyQualityLease

VERSION = 'reference-fidelity-generic-1'
RESULTS = ['Met', 'Unmet', 'Uncertain', 'NotObservable']


def extract(asset):
    value = requirements(asset)
    source = []
    for match in re.finditer(r'[^,;:.]+', asset['prompt']):
        quote = match.group(0).strip()
        if len(quote) < 4: continue
        kind = classify(quote)
        source.append(criterion(asset, 'RF'+str(len(source)+1).zfill(2), quote, quote, kind=kind,
            importance='critical' if kind == 'visual' and not any(r['visually_evaluable'] for r in source) else 'major'))
    value['requirements'] = source + [r for r in value['requirements'] if r['source']['field'] != 'prompt']
    value['reference_version'] = VERSION
    value['requirements_sha256'] = digest(value['requirements'])
    return value


def schema_for(criteria):
    item = obj({'result': enum(RESULTS), 'evidence': {'type': 'string', 'minLength': 1, 'maxLength': 350},
                'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1}, 'related_images': {'type': 'array', 'items': enum(['reference']), 'minItems': 1, 'maxItems': 1, 'uniqueItems': True},
                'uncertainty_reason': {'type': 'string', 'maxLength': 350}})
    return obj({'criteria': obj({r['id']: item for r in criteria if r['visually_evaluable']}),
                'summary': {'type': 'string', 'minLength': 1, 'maxLength': 600}})


def validate_evidence(value, criteria):
    expected = {r['id'] for r in criteria if r['visually_evaluable']}
    if set(value['criteria']) != expected: raise ValueError('Reference fidelity criterion set differs from source')
    for key, evidence in value['criteria'].items():
        if evidence['result'] in ('Uncertain', 'NotObservable') and not evidence['uncertainty_reason'].strip(): raise ValueError('Unknown evidence requires an explicit reason')
        if evidence['result'] == 'Met' and evidence['uncertainty_reason'].strip(): raise ValueError('Contradictory met/uncertainty evidence')
        if evidence['related_images'] != ['reference']: raise ValueError('Only supplied reference image can be cited')
    return True


def fidelity_decision(criteria):
    critical = [r for r in criteria if r['visually_evaluable'] and r['importance'] == 'critical']
    if any(r['result'] == 'Unmet' and r['confidence'] >= .75 for r in critical): return 'FAIL'
    if any(r['result'] != 'Met' or r['confidence'] < .75 for r in critical): return 'NeedsReview'
    return 'PASS'


def summarize(criteria):
    visual = [r for r in criteria if r['visually_evaluable']]
    weight = {'critical': 3, 'major': 2, 'minor': 1}
    def score(items):
        total = sum(weight[r['importance']] for r in items)
        return round(100*sum(weight[r['importance']] for r in items if r['result'] == 'Met')/total, 3) if total else None
    return {'requirement_fidelity': score([r for r in visual if r['category'] == 'prompt_fidelity']),
            'style_consistency': score([r for r in visual if r['category'] == 'art_style']),
            'silhouette_quality': score([r for r in visual if r['category'] in ('geometry', 'top_down_readability')]),
            'critical_unmet': [r['id'] for r in visual if r['importance'] == 'critical' and r['result'] == 'Unmet'],
            'major_unmet': [r['id'] for r in visual if r['importance'] == 'major' and r['result'] == 'Unmet'],
            'unknown_count': sum(r['result'] in ('Uncertain', 'NotObservable') for r in visual)}


def current_fidelity(db, store, job_id):
    row = store.db.execute('SELECT report_json FROM ReferenceFidelityReports WHERE job_id=? ORDER BY created_at DESC LIMIT 1', (job_id,)).fetchone()
    if not row: return None
    r = json.loads(row[0]); asset = json.loads(db.job(job_id)['request_json'])
    from .quality_config import load_quality
    config = load_quality(); source = outputs_for(db, job_id).get('ReferenceGenerated', {}).get('reference')
    if r.get('error') or r.get('version') != VERSION or r.get('source') != source or not source or not intact(source): return None
    if r['requirements_sha256'] != extract(asset)['requirements_sha256'] or r['vlm_profile'] != config: return None
    if not all(intact(a) for a in r.get('artifacts', [])): return None
    return r


class ReferenceFidelityEvaluator:
    def __init__(self, db, store, vlm=None, lease_factory=DualComfyQualityLease):
        self.db = db; self.store = store; self.vlm = vlm or OllamaVLM(); self.lease_factory = lease_factory

    def evaluate(self, job_id, parent_lock=None):
        cached = current_fidelity(self.db, self.store, job_id)
        if cached: return {**cached, 'cache_reused': True}
        job = self.db.job(job_id); asset = json.loads(job['request_json']); criteria = extract(asset)
        source = outputs_for(self.db, job_id).get('ReferenceGenerated', {}).get('reference')
        if not source or not intact(source): raise ProductionError('ReferenceIntegrity', 'Original reference PNG required')
        ident = uuid.uuid4().hex; work = self.store.output_root/'ReferenceFidelity'/job_id/ident; work.mkdir(parents=True)
        started = time.monotonic(); r = {'report_id': ident, 'job_id': job_id, 'version': VERSION, 'source': source, 'source_asset_sha256': digest(asset),
            'requirements_sha256': criteria['requirements_sha256'], 'vlm_profile': self.vlm.profile, 'created_at': utcnow(), 'work_dir': str(work),
            'status': 'NeedsReview', 'confidence_is_probability': False, 'automatic_approval': False}
        atomic_json(work/'requirements.json', criteria)
        try:
            metadata = self.vlm.metadata(); atomic_json(work/'model-metadata.json', metadata)
            with self.lease_factory(self.vlm, work, parent_lock=parent_lock) as lease:
                lease.mark_owned()
                prompt = ('Evaluate this ORIGINAL reference PNG, not a 3D mesh and not an alpha mask. Image ID reference. '
                    'Keep summary to ONE short sentence, maximum 180 characters. Each evidence is one brief sentence, maximum 150 characters. Do not repeat the criterion list in summary. '
                    'Judge each source-anchored requirement independently. Identity alone does not establish materials/details. '
                    'A sloped grey roof does not by itself prove tin or corrugation. Wooden doors are not fabric curtains. '
                    'Do not invent details hidden in dark interiors. Cite visible evidence specific to each material or part. '
                    'Use Uncertain/NotObservable with explicit reason for occluded or ambiguous details. Never infer physical dimensions or occupant capacity. '
                    'The source is requested design, not observed fact. Return all listed visual criterion IDs. Source criteria: '+
                    json.dumps([r for r in criteria['requirements'] if r['visually_evaluable']], ensure_ascii=False))
                value, metrics = self.vlm.structured(prompt, schema_for(criteria['requirements']), work, 'reference-fidelity', images=[source['path']], validate_value=lambda v: validate_evidence(v, criteria['requirements']))
                placement = lease.placement()
            evaluated = []
            for item in criteria['requirements']:
                evidence = value['criteria'].get(item['id'])
                evaluated.append({**item, **evidence} if evidence else {**item, 'result': 'NotObservable', 'evidence': 'Requires numeric mesh measurement or nonvisual information; not provable from PNG', 'confidence': None, 'related_images': [], 'uncertainty_reason': 'Nonvisual source requirement'})
            status = fidelity_decision(evaluated)
            if not placement.get('verified') or placement.get('cpu_offload_detected'): status = 'NeedsReview'
            r.update(status=status, requirements=evaluated, summary=value['summary'], metrics=summarize(evaluated), performance=metrics, model=self.vlm.model, model_digest=metadata['installed']['digest'], model_placement=placement)
        except Exception as e: r['error'] = {'category': getattr(e, 'category', type(e).__name__), 'message': str(e)}
        r['elapsed_seconds'] = time.monotonic()-started
        r['artifacts'] = [file_info(p) for p in work.rglob('*') if p.is_file()]
        atomic_json(work/'reference-fidelity.json', r)
        key = digest({'source': source, 'requirements': criteria['requirements_sha256'], 'profile': self.vlm.profile, 'version': VERSION})
        self.store.db.execute('INSERT INTO ReferenceFidelityReports VALUES (?,?,?,?,?,?,?)', (ident, job_id, key, r['status'], str(work), canonical(r), r['created_at'])); self.store.db.commit()
        return r
