import importlib.util
from .common import AI, ProductionError

spec=importlib.util.spec_from_file_location('g3dc_prompt_guard',AI/'Scripts/prompt_guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)

def prepare(asset,settings):
    original=settings.get('prompt') or asset['prompt']
    language=settings.get('prompt_language',asset.get('prompt_language','en'))
    attempts=[]
    try:
        text=guard.translate_prompt(original,log=attempts) if language=='tr' else original.strip()
    except guard.PromptTranslationError as exc:
        error=ProductionError('PromptTranslation',str(exc))
        error.prompt_record={'original':original,'language':language,'translation_attempts':attempts,'result':'rejected'}
        raise error from exc
    if not text:raise ProductionError('PromptTranslation','Empty asset prompt.')
    if '{SIZE}' in text:
        footprint=settings.get('footprint_m',asset.get('footprint_m'))
        if not footprint:raise ProductionError('CatalogAmbiguity','SIZE requires a footprint override.')
        text=text.replace('{SIZE}',f'{footprint[0]:g} x {footprint[1]:g} meters')
    suffix=settings.get('variant_prompt','').strip()
    style=settings.get('style',asset['shared_style'])
    negative=settings.get('negative',asset['shared_negative'])
    return {'original':original,'translated':text,'optimized':'\n'.join(filter(None,[text,suffix,style,
            'Exterior three-quarter view, entire object visible, isolated on a plain white background.'])),
            'negative':negative,'language':language,'translation_used':language=='tr','translation_attempts':attempts,
            'negative_applied_to':['DreamShaper CLIP negative conditioning'],
            'negative_not_supported_by':['Hunyuan Shape input','Hunyuan PBR input'],
            'warnings':['Semantic prompt adherence and artistic quality are not automatically verified.']}
