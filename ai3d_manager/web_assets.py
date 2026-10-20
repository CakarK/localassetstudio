"""User-authored assets use explicit style and dimensions with existing gate profiles."""
import math
import re
import unicodedata
from .catalog import load_catalog

PROFILES={'building':('Buildings',8000),'shelter':('Buildings',4500),'tower':('Buildings',8000),
          'tree':('Environment',4000),'fence':('Environment',2000),'debris':('Environment',3000),'prop':('Environment',2000)}

def asset_from_request(data):
    if data.get('source_mode','catalog')=='catalog':
        asset=next((a for a in load_catalog() if a['asset_id']==data.get('asset_id')),None)
        if not asset:raise ValueError('Katalog varlığı bulunamadı.')
        return asset
    if data.get('source_mode')!='custom':raise ValueError('Geçersiz varlık kaynağı.')
    name=data.get('name','');prompt=data.get('prompt','');language=data.get('prompt_language','tr');profile=data.get('gate_profile','prop')
    if not isinstance(name,str) or not 1<=len(name.strip())<=100:raise ValueError('Yeni varlık için 1–100 karakter ad girin.')
    if not isinstance(prompt,str) or not 8<=len(prompt.strip())<=1400:raise ValueError('Prompt 8–1400 karakter olmalı.')
    if language not in ('tr','en') or profile not in PROFILES:raise ValueError('Geçersiz dil veya varlık türü.')
    slug=re.sub('[^A-Za-z0-9]+','_',unicodedata.normalize('NFKD',name).encode('ascii','ignore').decode()).strip('_')[:60] or 'Asset'
    target=data.get('polygon_target',PROFILES[profile][1])
    if type(target)!=int or not 100<=target<=100000:raise ValueError('Üçgen hedefi 100–100000 arasında olmalı.')
    footprint=data.get('footprint_m',[1,1]);height=data.get('height_m')
    def dimension(v):return type(v) in (int,float) and math.isfinite(v) and .1<=v<=512
    if not isinstance(footprint,list) or len(footprint)!=2 or not all(dimension(v) for v in footprint):raise ValueError('Genişlik/derinlik 0,1–512 metre olmalı.')
    if height is not None and not dimension(height):raise ValueError('Yükseklik 0,1–512 metre olmalı veya boş bırakılmalı.')
    period=data.get('period')
    if period is not None and (not isinstance(period,str) or len(period)>80):raise ValueError('Geçersiz dönem etiketi.')
    style=data.get('shared_style','');negative=data.get('shared_negative','')
    if not isinstance(style,str) or len(style)>2000 or not isinstance(negative,str) or len(negative)>2000:
        raise ValueError('Stil/negatif prompt en fazla 2000 karakter olmalı.')
    return {'asset_id':'Custom_'+slug,'name_tr':name.strip(),'name_en':name.strip(),'prompt':prompt.strip(),
        'prompt_language':language,'category':PROFILES[profile][0],'period':period,
        'phase35_gate_profile':profile,'shared_style':style,'shared_negative':negative,
        'user_shared_style':style,'user_shared_negative':negative,
        'grid':None,'footprint_m':footprint,'height_m':height,'texture_size':1024,
        'polygon_target':target,'polygon_range':[max(100,int(target*.75)),int(target*1.25)],
        'source':{'kind':'web_custom_prompt'},'origins':{'prompt':'user','dimensions':'user','polygon_budget':'user','style':'user'},
        'warnings':[],'web_custom_asset':True}
