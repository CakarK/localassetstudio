import truststore
truststore.inject_into_ssl()
from pathlib import Path
import os, json, hashlib
ROOT=Path(__file__).resolve().parents[1]
os.environ['HF_HOME']=str(ROOT/'Models'/'huggingface')
from huggingface_hub import HfApi,hf_hub_download
api=HfApi()
repo='tencent/Hunyuan3D-2.1'; revision='0b94677654c57bb9a6b6845cd7b704ccf551d327'
manifest=[]
info=api.model_info(repo,revision=revision,files_metadata=True)
for s in info.siblings:
    name=s.rfilename
    if not (name in ['LICENSE','Notice.txt'] or name.startswith('hunyuan3d-paintpbr-v2-1/') or name in ['hunyuan3d-dit-v2-1/model.fp16.ckpt','hunyuan3d-vae-v2-1/model.fp16.ckpt']): continue
    print('DOWNLOAD',name,flush=True)
    p=Path(hf_hub_download(repo,name,revision=revision))
    expected=s.lfs.sha256 if s.lfs else None
    actual=None
    if expected:
        h=hashlib.sha256()
        with p.open('rb') as f:
            for b in iter(lambda:f.read(8*1024*1024),b''): h.update(b)
        actual=h.hexdigest()
        if actual!=expected: raise RuntimeError('SHA256 mismatch '+name)
    if name.endswith('.ckpt'):
        kind='vae' if 'vae' in name else 'diffusion_models'
        dest=ROOT/'ComfyUI'/'models'/kind/('hunyuan3d-'+kind+'-v2-1-fp16.ckpt')
        dest.parent.mkdir(parents=True,exist_ok=True)
        # Windows hardlink must target the resolved blob, not the HF symlink.
        if dest.is_symlink() and not dest.exists(): dest.unlink()
        if not dest.exists(): os.link(p.resolve(),dest)
    manifest.append(dict(repository=repo,revision=revision,file=name,size=p.stat().st_size,sha256=actual,expected_sha256=expected,path=str(p)))
    (ROOT/'Models'/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print('HUNYUAN DOWNLOAD COMPLETE',flush=True)
# Precache DINO used by the PBR network; pin its current revision.
dino=api.model_info('facebook/dinov2-giant',revision='611a9d42f2335e0f921f1e313ad3c1b7178d206d',files_metadata=True)
for s in dino.siblings:
    if s.rfilename not in ['config.json','preprocessor_config.json','model.safetensors']: continue
    print('DOWNLOAD DINO',s.rfilename,flush=True)
    p=Path(hf_hub_download('facebook/dinov2-giant',s.rfilename,revision=dino.sha))
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''): h.update(b)
    expected=s.lfs.sha256 if s.lfs else None
    if expected and h.hexdigest()!=expected: raise RuntimeError('DINO SHA mismatch')
    manifest.append(dict(repository='facebook/dinov2-giant',revision=dino.sha,file=s.rfilename,path=str(p),size=p.stat().st_size,sha256=h.hexdigest(),expected_sha256=expected))
(ROOT/'Models'/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print('ALL MODELS VERIFIED',flush=True)
