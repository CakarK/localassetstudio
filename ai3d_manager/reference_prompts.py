"""Versioned generation composition; original catalog/style remain authoritative."""
from .common import digest
VERSION='whole-object-reference-generic-1'

def compile_prompt(asset):
    original=asset['prompt'];style=asset['shared_style'];negative=asset['shared_negative']
    text='One complete '+(asset.get('name_en') or asset['asset_id'])+', entire object centered with empty margins, no cropping. '+original
    phrases=[original]
    negative+=', close-up roof, single wall, detached building fragment, flat metal sheet, architectural section, extreme crop, macro photography, floating geometry, raised presentation pedestal, thick rectangular display base, multiple buildings, unrelated environment, cutaway model'
    return {'version':VERSION,'original_prompt':original,'generation_prompt':text,'shared_style':style,'original_negative':asset['shared_negative'],'negative':negative,
            'preserved_source_phrases':phrases,'source_sha256':digest({k:asset.get(k) for k in ('prompt','shared_style','shared_negative','period','grid')}),
            'priority':['identity','whole body','complete silhouette','camera/framing','materials','required details','small decoration'],
            'composition_additions':['Whole-object framing and margins','Exterior body/roof/entrance continuity'],
            'negative_applied_to':['DreamShaper CLIP negative conditioning'],'no_hunyuan_text_conditioning':True}
