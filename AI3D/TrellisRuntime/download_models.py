"""Download only pinned, commercially permitted public weights for one 512 trial.

DINOv3 is deliberately separate and requires the user's approved HF credentials.
"""
import argparse
import hashlib
import json
import os
import ssl
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
# Use the Windows trust store, including locally managed roots. TLS verification
# stays enabled; never use verify=False or an unverified SSL context.
bundle = ROOT / 'windows-ca-bundle.pem'
certs = []
for store in ('ROOT', 'CA'):
    for der, encoding, trust in ssl.enum_certificates(store):
        if encoding == 'x509_asn':
            certs.append(ssl.DER_cert_to_PEM_cert(der))
bundle.write_text(''.join(certs), encoding='ascii')
os.environ['REQUESTS_CA_BUNDLE'] = str(bundle)
os.environ['HF_HUB_DISABLE_XET'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['HF_HUB_DOWNLOAD_TIMEOUT'] = '120'
from huggingface_hub import hf_hub_download, get_token

PLAN = json.loads((ROOT / "model-tree-plan.json").read_text())
CHECKPOINTS = ["shape_dec_next_dc_f16c32_fp16", "tex_dec_next_dc_f16c32_fp16",
               "ss_flow_img_dit_1_3B_64_bf16", "slat_flow_img2shape_dit_1_3B_512_bf16",
               "slat_flow_imgshape2tex_dit_1_3B_512_bf16"]

def required(model, files):
    names = {"README.md"}
    if model == "microsoft/TRELLIS.2-4B":
        names.add("pipeline.json")
        for ckpt in CHECKPOINTS:
            names.update({"ckpts/" + ckpt + ".json", "ckpts/" + ckpt + ".safetensors"})
    elif model == "microsoft/TRELLIS-image-large":
        names.update({"ckpts/ss_dec_conv3d_16l8_fp16.json", "ckpts/ss_dec_conv3d_16l8_fp16.safetensors"})
    elif model == "ZhengPeng7/BiRefNet":
        names.update({"model.safetensors", "config.json", "birefnet.py", "BiRefNet_config.py"})
    else:
        names.update({"model.safetensors", "config.json", "preprocessor_config.json", "LICENSE.md"})
    return [f for f in files if f.get("type") == "file" and f["path"] in names]

def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b""):h.update(chunk)
    return h.hexdigest()

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--dino",action="store_true")
    args=parser.parse_args()
    credential=get_token() if args.dino else False
    if args.dino and not credential:
        from huggingface_hub import login
        login()
        credential=get_token()
    if args.dino and not credential:
        raise SystemExit("DINOv3 access pending: log in locally with an approved HF account. No token should be shared in chat.")
    expected_files={ (f["model"],f["file"]):f for f in json.loads((ROOT/"model-files.lock.json").read_text())["files"]}
    records=[]
    started=time.monotonic()
    for model in PLAN:
        is_dino=model["model"].startswith("facebook/")
        if is_dino != args.dino:
            continue
        files=required(model["model"],model["files"])
        print("MODEL",model["model"],"files",len(files),"bytes",sum(f['size'] for f in files),flush=True)
        folder=ROOT/"models"/model["model"].replace("/","__")
        for f in files:
            before=time.monotonic()
            path=hf_hub_download(model["model"],f["path"],revision=model["revision"],local_dir=folder,token=credential)
            actual=sha256(path)
            locked=expected_files[(model["model"],f["path"])]
            if actual!=locked["sha256"] or path.stat().st_size!=locked["bytes"]:raise RuntimeError("Pinned model hash mismatch: "+f["path"])
            expected=f.get("lfs",{}).get("oid")
            if Path(path).stat().st_size!=f["size"] or (expected and expected!=actual):
                raise RuntimeError("Downloaded model hash/size mismatch: "+f["path"])
            records.append({"model":model["model"],"revision":model["revision"],"file":f["path"],
                            "path":str(Path(path).resolve()),"bytes":f["size"],"sha256":actual,
                            "publisher_sha256":expected,"measured_seconds":time.monotonic()-before})
            print("VERIFIED",f["path"],f["size"],"bytes",flush=True)
            (ROOT/("dino-downloads.json" if args.dino else "public-model-downloads.json")).write_text(json.dumps({"files":records,"elapsed_seconds":time.monotonic()-started},indent=2),encoding="utf-8")
    print("Download and verification complete",round(time.monotonic()-started,2),"seconds",flush=True)
