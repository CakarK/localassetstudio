"""Existing isolated SAM package, CPU only, source-derived prompt. No downloads."""
import argparse,json
from pathlib import Path
import numpy as np
from PIL import Image
import torch
from segment_anything import sam_model_registry,SamPredictor
p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--seed-mask',required=True);p.add_argument('--checkpoint',required=True);p.add_argument('--output',required=True);a=p.parse_args()
torch.set_num_threads(8)
rgb=np.array(Image.open(a.source).convert('RGB'));seed=np.array(Image.open(a.seed_mask).convert('RGBA'))[:,:,3]>127
yy,xx=np.nonzero(seed)
if not len(xx):raise ValueError('No source-derived prompt; semantic foreground not invented')
box=np.array([xx.min(),yy.min(),xx.max(),yy.max()]);model=sam_model_registry['vit_b'](checkpoint=a.checkpoint).to('cpu');predictor=SamPredictor(model);predictor.set_image(rgb)
masks,scores,_=predictor.predict(box=box,multimask_output=True)
index=int(np.argmax(scores));rgba=np.concatenate([rgb,np.where(masks[index],255,0).astype('uint8')[:,:,None]],axis=2);Image.fromarray(rgba).save(a.output)
Path(a.output).with_suffix('.sam.json').write_text(json.dumps({'backend':'SAM ViT-B','device':'CPU','box_xyxy':box.tolist(),'box_origin':'source RGB adaptive mask bounding extent, not semantic ground truth',
    'sam_predicted_iou':scores.tolist(),'selected_index':index,'predicted_iou_is_calibrated_quality':False,'rgb_generated':False,'torch':torch.__version__},indent=2),'utf-8')
