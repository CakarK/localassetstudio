"""Explicit catalog mapping, opt-in active profiles, separate recommendations."""
import copy
import json
from .common import AI,ProductionError,digest

CONFIG=AI/'Config/category_profiles.json'
RECOMMENDATIONS=AI/'Config/category_recommendations.json'


def load_profiles(path=None):
    value=json.loads((path or CONFIG).read_text('utf-8'))
    from .image_providers import load_registry
    providers=load_registry()['providers']
    for name,p in value['profiles'].items():
        for key in ['preferred_provider','alternative_provider']:
            if p[key] is not None and (p[key] not in providers or providers[p[key]].get('research_only')):raise ValueError('Unavailable category provider')
        if p['segmentation_strategy'] not in ['existing-u2net','adaptive-border','adaptive-grabcut','alpha-preservation','local-sam-vit-b']:raise ValueError('Unknown segmentation strategy')
    return value


def resolve_profile(asset,config=None):
    c=config or load_profiles();ident=asset['asset_id']
    if ident in c['asset_profiles']:name=c['asset_profiles'][ident]
    else:
        name=asset.get('category_profile')
        if name is None:raise ValueError('Set category_profile or an explicit asset mapping for '+ident)
        if name not in c['profiles']:raise ValueError('Unknown category profile: '+str(name))
    if name is None:return {'name':None,'asset_id':ident,'status':'NeedsReview','reason':'Character pipeline is outside these eight static-asset profiles'}
    p=copy.deepcopy(c['profiles'][name]);p.update(name=name,asset_id=ident,status='Configured',config_sha256=digest(c))
    p['polygon_budget']={'target':asset['polygon_target'],'range':asset.get('polygon_range'),'source':'unchanged catalog asset'}
    p['texture_preset']={'size':asset['texture_size'],'maps':['albedo','roughness','metallic'],'source':'unchanged catalog asset'}
    # Shelter and named trees keep their more specific pre-existing gate profile.
    from .gate_analysis import profile_for
    old=profile_for({k:v for k,v in asset.items() if k!='phase35_gate_profile'})['name']
    if old!='unknown':p['reference_gate_profile']=p['geometry_gate_profile']=old
    return p


def active_provider(asset,settings,config=None):
    c=config or load_profiles()
    if not settings.get('category_profile') and not c['active']:return None
    p=resolve_profile(asset,c)
    if p['name'] is None:raise ProductionError('CategoryNeedsReview',p['reason'])
    requested=settings.get('category_profile','auto')
    if requested not in ['auto',p['name']]:raise ValueError('Requested profile conflicts with exact catalog mapping')
    return p['preferred_provider']


def apply_profile(asset,settings,config=None):
    c=config or load_profiles()
    if not settings.get('category_profile') and not c['active']:return asset,settings
    p=resolve_profile(asset,c);active_provider(asset,settings,c)
    result=copy.deepcopy(asset);opts=dict(settings)
    result['category_pipeline']=p;result['phase35_gate_profile']=p['reference_gate_profile']
    opts.setdefault('segmentation_strategy',p['segmentation_strategy'])
    return result,opts


def prompt_for(asset):
    p=asset.get('category_pipeline')
    if not p:return None
    return p['prompt_template'].format(prompt=asset['prompt'],camera=p['camera_composition'])


def recommend(category):
    recommendations=json.loads(RECOMMENDATIONS.read_text('utf-8'))
    if category not in recommendations['profiles']:raise ValueError('Unknown exact category')
    c=load_profiles()
    return {'category':category,'recommendation':recommendations['profiles'][category],
            'active_configuration':c['profiles'][category],'global_active':c['active'],
            'automatic_default_changed':False,'calibrated_category_winner':False}
