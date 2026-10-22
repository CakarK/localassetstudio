"""Check free gated DINOv3 access before downloading large image/3D weights."""
import json
from pathlib import Path

PAGE='https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m'

def require_access(url,token_getter,login,metadata,open_page):
    token=token_getter()
    if not token:
        print('DINOv3: accept the free access terms using your own Hugging Face account: '+PAGE,flush=True)
        open_page(PAGE)
        login()
        token=token_getter()
    if not token:raise RuntimeError('Local Hugging Face login is required; no weights were downloaded')
    metadata(url,token=token)  # HEAD/metadata only, never a model download.
    return {'status':'PASS','gated_access_verified':True,'model_downloaded':False}

def main():
    import webbrowser
    from huggingface_hub import get_token,login,get_hf_file_metadata,hf_hub_url
    records=json.loads((Path(__file__).parent/'model-files.lock.json').read_text('utf-8'))['files']
    spec=next(f for f in records if f['model'].startswith('facebook/dinov3') and f['file']=='model.safetensors')
    url=hf_hub_url(spec['model'],spec['file'],revision=spec['revision'])
    try:print(json.dumps(require_access(url,get_token,login,get_hf_file_metadata,webbrowser.open)))
    except Exception:
        raise SystemExit('DINOv3 access/login is not ready. Accept terms and obtain approval at '+PAGE+' then double-click setup again. No access control was bypassed.')

if __name__=='__main__':main()
