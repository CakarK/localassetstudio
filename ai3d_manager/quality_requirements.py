"""Versioned, source-anchored requirements; numeric facts never come from pixels."""
import re
from .common import digest

VERSION='requirements-generic-1'
CATEGORIES=['prompt_fidelity','art_style','geometry','texture','top_down_readability']

def criterion(asset,ident,description,quote,field='prompt',kind='visual',importance='major',category='prompt_fidelity'):
    text=asset[field];start=text.find(quote)
    if start<0:raise ValueError('Criterion quote is not in original source: '+quote)
    return {'id':ident,'description':description,'importance':importance,'type':kind,'category':category,
            'visually_evaluable':kind=='visual','source':{'field':field,'quote':quote,'start':start,'end':start+len(quote)},
            'result':'NotEvaluated','confidence':None}

def requirements(asset,ai=None):
    result=[]
    # Every criterion is quoted from this request, never from a named game asset.
    for i,match in enumerate(re.finditer(r'[^.;\n]+[.;]?',asset['prompt'])):
        quote=match.group(0).strip()
        if len(quote)<5:continue
        kind=classify(quote)
        result.append(criterion(asset,f'PF{i+1:02d}',quote,quote,kind=kind,importance='major'))
    for i,match in enumerate(re.finditer(r'[^.;\n]+[.;]?',asset.get('shared_style',''))):
        quote=match.group(0).strip()
        if len(quote)>=5:result.append(criterion(asset,f'ST{i+1:02d}',quote,quote,'shared_style',category='art_style'))
    shared=[('GE01','No unnecessary ground or base platform','no ground plane','shared_negative','geometry'),
            ('GE02','No floating detached pieces','no floating parts','shared_negative','geometry')]
    for ident,description,quote,field,category in shared:
        if quote in asset.get(field,''):result.append(criterion(asset,ident,description,quote,field,category=category))
    if asset.get('ground_free_required'):
        if not any(item['id']=='GE01' for item in result) and 'No ground mesh' in asset.get('shared_style',''):
            result.append(criterion(asset,'GE01','No unnecessary ground or base platform','No ground mesh','shared_style',category='geometry'))
        for item in result:
            if item['id']=='GE01':
                item['importance']='critical'
                item['description']='Object geometry only: no attached terrain tile, soil patch, ground mesh, ground/floor plane, base plate, plinth, pedestal or display platform. Check buildings and tree roots as strictly as other objects; retain the actual object itself.'
    if ai:
        for i,item in enumerate(ai['requirements']):
            field=item['field'];quote=item['quote'];kind=classify(quote,item['type'])
            # AI cannot promote count/capacity/dimensions into visible proof.
            item=criterion(asset,f'AI{i+1:02d}',item['description'],quote,field,kind,item['importance'])
            if not any(r['source']['quote']==quote and r['type']==kind for r in result):result.append(item)
    return {'version':VERSION,'source_sha256':digest({k:asset.get(k) for k in ('prompt','shared_style','shared_negative','grid','period')}),
            'engine':'source_anchored_template_plus_validated_AI' if ai else 'source_anchored_template',
            'interpretation_sha256':digest(result),'requirements':result}

def classify(quote,suggested='visual'):
    text=quote.lower()
    if re.search(r'\b(meters?|metres?|centimeters?|footprint|triangles?|polygons?|grid|\d+\s*x\s*\d+)\b',text):return 'technical'
    if re.search(r'\b(capacity|people|persons?|occupants?|survivors?|period|era|century)\b',text) and not re.search(r'no (people|persons|survivors)',text):return 'unverifiable'
    return suggested
