"""Shadow decisions. Evidence and confidence gates outrank the weighted score."""
from .quality_requirements import CATEGORIES

WEIGHTS=dict(zip(CATEGORIES,[.30,.25,.20,.15,.10]))
DEFAULT_POLICY={'weights':WEIGHTS,'approved_threshold':85,'review_threshold':70,'minimum_confidence':.70,
                'critical_confidence':.80,'max_unknown_fraction':.40,'mode':'shadow'}

def policy(overrides=None):
    p={**DEFAULT_POLICY,**(overrides or {})};weights=p['weights']
    if p['mode']!='shadow':raise ValueError('Only shadow visual decisions are enabled')
    if set(weights)!=set(CATEGORIES) or any(not isinstance(v,(int,float)) or not 0<=v<=100 for v in weights.values()) or sum(weights.values())<=0:raise ValueError('Invalid category weights')
    for key in ('minimum_confidence','critical_confidence','max_unknown_fraction'):
        if not 0<=p[key]<=1:raise ValueError('Invalid confidence policy')
    if not 0<=p['review_threshold']<p['approved_threshold']<=100:raise ValueError('Invalid score thresholds')
    return p

def score(payload,interpretation,technical,render_artifacts,config=None):
    config=policy(config);reqs=interpretation['requirements'];lookup={r['id']:r for r in reqs}
    views={v['view'] for v in render_artifacts if v.get('mode','material')=='material'}
    findings=[];results=[];reasons=[];unknown=0;visual_count=sum(r['visually_evaluable'] for r in reqs)
    valid_conf=[]
    for observation in payload['observations']:
        r=lookup[observation['criterion_id']];item={**r,**observation};evidence=set(item['views'])
        if not evidence<=views:raise ValueError('Unknown render evidence')
        # One hidden side is insufficient to prove absence across an object.
        if item['result'] in ('met','not_met') and (not evidence or item['confidence']<config['minimum_confidence']):item['result']='uncertain'
        if item['result']=='not_met' and len(evidence)<2:item['result']='uncertain'
        if item['result'] in ('uncertain','not_observable'):unknown+=1
        if r['importance']=='critical' and item['result'] in ('uncertain','not_observable'):reasons.append('Critical requirement not observable: '+r['id'])
        if r['importance']=='critical' and item['result']=='not_met' and item['confidence']<config['critical_confidence']:reasons.append('Critical failure remains uncertain: '+r['id'])
        valid_conf.append(item['confidence'])
        if item['result']=='not_met':
            findings.append({'criterion_id':r['id'],'category':r['category'],'problem':item['evidence'],
                'severity':item['severity'],'confidence':item['confidence'],'evidence_images':item['views'],
                'suggested_fix':item['suggested_fix'],'critical_prompt_requirement':r['importance']=='critical' and r['category']=='prompt_fidelity'})
        results.append(item)
    if {r['id'] for r in results}!={r['id'] for r in reqs if r['visually_evaluable']}:raise ValueError('Incomplete visual criterion coverage')
    technical_passed=technical.get('status') in ('PASS','WARN') and not any(c['status']=='FAIL' for c in technical.get('checks',[]))
    for r in reqs:
        if r['visually_evaluable']:continue
        if r['type']=='technical':
            check=next((c for c in technical.get('checks',[]) if c['check']=='footprint'),None)
            matched='footprint' in r['source']['quote'].lower() and check is not None
            results.append({**r,'result':check['status'] if matched else 'NotVerified','confidence':None,
                            'technical_evidence':check if matched else None})
        else:results.append({**r,'result':'UnverifiableFromRenders','confidence':None})
    scores={};denominator=sum(config['weights'].values())
    for category,data in payload['scores'].items():
        evidence=set(data['views']);scores[category]=data['score'] if evidence and evidence<=views else None
        if data['confidence']<config['minimum_confidence'] or not evidence:reasons.append('Low confidence/evidence in '+category)
    overall=round(sum(scores[c]*config['weights'][c] for c in CATEGORIES)/denominator,2) if all(v is not None for v in scores.values()) else None
    critical=[f for f in findings if (f['severity']=='critical' or f['critical_prompt_requirement']) and f['confidence']>=config['critical_confidence']]
    if not technical_passed:reasons.append('Technical validation failed or is unavailable')
    if payload['contradictions']:reasons.append('Conflicting visual evidence')
    if unknown/max(visual_count,1)>config['max_unknown_fraction']:reasons.append('Too many unobservable/uncertain requirements')
    if valid_conf and min(valid_conf)<config['minimum_confidence']:reasons.append('Low criterion confidence')
    if any(a.get('vlm_width',a.get('width',0))<512 for a in render_artifacts):reasons.append('Insufficient image resolution')
    decision='NeedsReview'
    if not reasons and overall is not None:
        decision='VisuallyApprovedCandidate' if overall>=config['approved_threshold'] else 'NeedsReview' if overall>=config['review_threshold'] else 'RejectedCandidate'
        if critical:decision='RejectedCandidate'
    return {'mode':'shadow','calibrated':False,'decision':decision,'technical_status':'TechnicalPassed' if technical_passed else 'TechnicalFailed',
            'visual_status':decision,'scores':scores,'category_evidence':payload['scores'],'overall_score':overall,
            'confidence':round(sum(valid_conf)/len(valid_conf),3) if valid_conf else None,'review_reasons':reasons,
            'requirements':results,'unverifiable_requirements':[r for r in results if r['type']=='unverifiable'],
            'findings':findings,'critical_findings':critical,'requested_detail_views':payload['requested_detail_views'],
            'contradictions':payload['contradictions'],'policy':config,
            'warning':'Uncalibrated VLM opinion. Not proof of semantic correctness; no production job status is changed.'}
