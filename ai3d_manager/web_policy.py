"""User's ground-free requirement reuses the existing GE01 visual evidence."""
from .common import ProductionError
import re

GROUND_PROMPT_VERSION=2

GROUND_STYLE=('Object geometry only, isolated on a plain white background. '
    'No ground mesh, terrain tile, soil patch, landscape, floor plane, base plate, plinth, pedestal or display platform.')
GROUND_NEGATIVE='ground mesh, terrain tile, soil patch, landscape, ground plane, floor plane, base plate, thick rectangular base, plinth, pedestal, display platform'

def ground_free_asset(asset):
    style=asset['shared_style'].replace('flat base on the ground','clean contact at the object\'s own lowest structural points')
    if asset.get('category')!='Buildings':style=style.replace('clear roof shape','clear object silhouette')
    specific=('Keep the complete tree trunk and natural roots without soil around them.' if asset.get('phase35_gate_profile')=='tree' or asset.get('category_profile')=='Organic' else
              'Keep the actual building complete without surrounding terrain.' if asset.get('category')=='Buildings' else 'Keep only the complete requested object without surrounding scenery.')
    return {**asset,'ground_free_required':True,'ground_free_prompt_version':GROUND_PROMPT_VERSION,'shared_style':style+'\n'+GROUND_STYLE+' '+specific,
        'shared_negative':asset['shared_negative']+', '+GROUND_NEGATIVE,
        'origins':{**asset.get('origins',{}),'ground_free_requirement':'explicit_user_instruction'}}

def reference_prompt(value,asset,settings):
    """Positive conditioning for isolated objects; retain the untouched source record.

    Z-Image has no negative conditioning. Repeating forbidden scenery in its
    positive text still conditions on that scenery. Only remove ground-related
    prohibitions here, never architectural requirements such as 'no windows'.
    """
    text=value['translated']
    text=re.sub(r'\bno\s+(?:surrounding\s+)?(?:ground(?:\s+(?:mesh|plane))?|terrain(?:\s+tile)?|soil(?:\s+patch)?|base(?:\s+(?:plate|plinth))?|display\s+(?:pedestal|platform)|floor\s+plane|plinth|pedestal)(?:\s+or\s+(?:soil|display\s+pedestal|ground\s+plane))?\b', '', text, flags=re.I)
    text=re.sub(r'\s*,\s*(?=[,.])', '', text)
    text=re.sub(r'([,.])(?:\s*[,])+', r'\1', text)
    tree=asset.get('phase35_gate_profile')=='tree' or asset.get('category_profile')=='Organic'
    outline=('The complete trunk and natural roots end directly against empty white space. White background is visible between and around the lowest roots.' if tree else
             'The exterior walls end at their own bottom edges. Empty white background touches each bottom wall edge and corner directly. The bottom outline follows the walls exactly.' if asset.get('category')=='Buildings' else
             'Empty white background touches the lowest edges of the object directly. The bottom silhouette follows the actual object exactly.')
    style=asset.get('shared_style','').split('\n'+GROUND_STYLE,1)[0]
    positive='\n'.join(filter(None,[
        'A single complete isolated object, cutout product illustration on uniform pure white.',
        text.strip(' ,.'),settings.get('variant_prompt','').strip(),outline,style,
        'Entire object visible with white margin on every side, including underneath.']))
    return {**value,'optimized':positive,'web_ground_prompt_version':GROUND_PROMPT_VERSION,
            'positive_conditioning_policy':'isolated_object_outline',
            'source_preserved':True}

def reference_targets(asset):
    # Legacy approved references keep their frozen checkpoints. New references
    # must pass the existing GE01 evidence before being offered for 3D selection.
    targets=['PromptPrepared','ReferenceGenerated']
    if asset.get('ground_free_prompt_version',0)>=GROUND_PROMPT_VERSION:
        targets+=['ReferenceFidelity','ReferenceProcessed','ReferenceApproved']
    return targets

def require_ground_free(report):
    """Do not reinterpret an unclear GE01 result as a ground-free pass."""
    criterion=next((r for r in report.get('requirements',[]) if r.get('id')=='GE01'),None)
    verified=bool(criterion and str(criterion.get('result','')).lower()=='met')
    if criterion and criterion.get('result') in ('Met','Unmet','Uncertain','NotObservable'):
        from .reference_fidelity import fidelity_decision
        verified=fidelity_decision([{**criterion,'visually_evaluable':True,'importance':'critical','confidence':criterion.get('confidence',0)}])=='PASS'
    if not verified:
        evidence=(criterion or {}).get('evidence','No verified GE01 ground/base evidence.')
        raise ProductionError('GroundFreeReview','Zemin/kaide içermediği doğrulanmadı; otomatik ilerleme engellendi. '+evidence)
