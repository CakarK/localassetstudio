"""Closed JSON schemas and dynamic evidence references."""
import json
import re
from jsonschema import Draft202012Validator
from .quality_requirements import CATEGORIES

def obj(properties):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}
def enum(values):return {'type':'string','enum':list(values)}
STRING={'type':'string','minLength':1,'maxLength':1600}
CONFIDENCE={'type':'number','minimum':0,'maximum':1}
EXTRACTION=obj({'requirements':{'type':'array','minItems':1,'maxItems':20,'items':obj({
    'description':STRING,'quote':STRING,'field':enum(['prompt','shared_style','shared_negative']),
    'type':enum(['visual','technical','unverifiable']),'importance':enum(['critical','major','minor'])})}})

def response_schema(requirements,views):
    ids=[r['id'] for r in requirements if r['visually_evaluable']]
    evidence={'type':'array','maxItems':len(views),'uniqueItems':True,'items':enum(views)}
    score=obj({'score':{'type':'number','minimum':0,'maximum':100},'confidence':CONFIDENCE,'views':evidence,'reason':STRING})
    return obj({'scores':obj({c:score for c in CATEGORIES}),
        'observations':{'type':'array','minItems':len(ids),'maxItems':len(ids),'items':obj({
          'criterion_id':enum(ids),'result':enum(['met','not_met','uncertain','not_observable']),
          'confidence':CONFIDENCE,'views':evidence,'evidence':STRING,'severity':enum(['critical','major','minor','info']),
          'suggested_fix':{'type':'string','maxLength':1600}})},
        'contradictions':{'type':'array','maxItems':20,'items':STRING},
        'requested_detail_views':{'type':'array','maxItems':20,'items':STRING}})

def parse_response(response,schema):
    if not isinstance(response,dict) or response.get('done') is not True or response.get('done_reason') not in (None,'stop'):
        raise ValueError('Incomplete VLM response; no score accepted')
    content=response.get('message',{}).get('content')
    if not isinstance(content,str) or '<think' in content.lower():raise ValueError('VLM response must contain JSON only')
    # Some local runners return a single Markdown JSON fence even with format.
    # Permit only that entire wrapper; reject prose, extra blocks or trailing data.
    fenced=re.fullmatch(r'\s*```(?:json)?\s*\n([\s\S]*?)\n```\s*',content)
    if fenced:content=fenced.group(1)
    value=json.loads(content,parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
    Draft202012Validator(schema).validate(value)
    if 'observations' in value:
        ids=[o['criterion_id'] for o in value['observations']]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate criterion observations')
        if any(o['result']=='not_met' and not o['suggested_fix'].strip() for o in value['observations']):raise ValueError('A failed criterion requires a proposed correction')
    return value
