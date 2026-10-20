"""Resolve design edits into one English request, using the existing local CPU model."""
import re
import json
from .prompts import guard

def compose(asset,edits,*,review_feedback=False):
    # Remove source clauses superseded by explicit component edits before composition.
    # This prevents a small translation model from copying both the old and new roof.
    components=[]
    roof_change=re.search(r'replace the roof|change the roof|(?:çatı|cati).{0,90}(?:değiş|degis|kiremit|branda|sac|metal|eğim|düz)|roof.{0,80}(?:tile|canvas|metal|flat|pitched|timber|corrugated)|(?:tile|canvas|flat|pitched).{0,30}roof',edits,re.I)
    door_change=re.search(r'replace the entrance|change the door|(?:kapı|kapi|giriş).{0,90}(?:ahşap|ahs|metal|perde|geniş|dar|büyük|küçük|çift|değiş|degis)|(?:door|entrance).{0,80}(?:wood|metal|curtain|wide|double|open|bigger|smaller)|(?:wooden|metal|curtain|wide).{0,30}door',edits,re.I)
    if roof_change:components.append(r'\broof\b')
    if door_change:components.append(r'\bdoor\b|\bentrance\b|\bopen front\b')
    clear_environment=bool(re.search(r'no surrounding clutter|remove.*(?:barrels|crates)|(?:varil|sandık).*?(?:kaldır|olmasın|istemiyorum)',edits,re.I))
    if clear_environment:components.append(r'\bcrates?\b|\bbarrels?\b|\bweeds\b|\boutside\b')
    clauses=[];superseded=[]
    for clause in re.split(r',|[.;]\s+(?=[A-Za-z])',re.sub(r'\s+',' ',asset['prompt']).strip().rstrip('.')):
        clause=clause.strip()
        if any(re.search(pattern,clause,re.I) for pattern in components) and (clear_environment or not re.search(r'\b(above|beside|by|near) the (door|entrance)\b',clause,re.I)):
            superseded.append(clause)
            if ' with ' in clause:
                prefix=clause.rsplit(' with ',1)[0]
                if not any(re.search(pattern,prefix,re.I) for pattern in components):clauses.append(prefix)
        else:clauses.append(clause)
    source={'unchanged_catalog_details':', '.join(clauses)+'.','design_edits':edits,'footprint_m':asset.get('footprint_m'),'height_m':asset.get('height_m')}
    attempts=[]
    # Translate edits only. Asking a small model to rewrite the whole source can
    # silently discard a requested door or reintroduce the superseded tin roof.
    if review_feedback:
        def feedback_post(url,**kwargs):
            # Reuse the same local CPU model, schema, timeouts, validation and
            # bounded attempts. Review complaints describe faults, whereas an
            # image prompt must describe the desired corrected object.
            body=kwargs['json']
            body['options']['num_ctx']=4096
            body['messages']=[{'role':'system','content':
                'Convert human asset-review feedback into concise English design requirements for the corrected object. '
                'Return JSON with ONLY prompt. No reasoning or commentary. '
                'Describe the desired final result, never restate an observed defect as a positive requirement. '
                'Preserve every actionable change and all materials, quantities, placement and directions. '
                'For example: "doors on both sides; only on the right" means "Doors exclusively on the right-hand side; the left side has continuous panels without doors." '
                '"No material; add stone" means "Visible weathered stone materials with realistic roughness." '
                'A vague rejection without a concrete edit means a new variant of the same asset, preserving identity and style; do not invent specific changes. '
                'Do not add scenery, terrain, a ground slab or pedestal.'},
                {'role':'user','content':json.dumps({'asset':asset.get('name_en',asset.get('name_tr',asset['asset_id'])),'review_feedback':edits},ensure_ascii=False)}]
            return guard.requests.post(url,**kwargs)
        translated=guard.translate_prompt(edits,log=attempts,post=feedback_post)
    else:translated=guard.translate_prompt(edits,log=attempts)
    prompt=source['unchanged_catalog_details']+'\nDesign requirements: '+translated
    return {'prompt':prompt,'original_catalog_prompt':asset['prompt'],'edits':edits,
            'translated_edits':translated,'model':'qwen3:4b','device':'CPU','superseded_catalog_clauses':superseded,'composition_source':source,'attempts':attempts,
            'review_feedback_resolved':review_feedback}
