"""CLI for the same ComfyUI graphs; resumes from saved reference or geometry."""
import argparse
import json
import shutil
from pathlib import Path
import requests
from workflows import text_graph,image_graph,shape_graph,with_paint,node
from run_workflow import run,ROOT,URL

p=argparse.ArgumentParser()
p.add_argument('--prompt',default='')
p.add_argument('--negative',default='cropped, multiple objects, people, text, watermark, complex background')
p.add_argument('--reference',type=Path)
p.add_argument('--project',default='asset_001')
p.add_argument('--preset',choices=['low','environment','hero'],default='environment')
p.add_argument('--mode',choices=['geometry','pbr'],default='geometry')
p.add_argument('--resume',choices=['image','shape','paint'])
p.add_argument('--seed',type=int,default=42)
p.add_argument('--polygons',type=int)
p.add_argument('--texture-size',type=int,choices=[512,1024,2048,4096])
p.add_argument('--format',choices=['glb','obj'],default='glb')
p.add_argument('--height-m',type=float,default=1.0)
p.add_argument('--output-directory',type=Path)
args=p.parse_args()
presets=json.loads((ROOT/'presets.json').read_text('utf-8'))
cfg=presets[args.preset]
if args.resume:
    path=ROOT/'Outputs'/args.project/'preprocessed.png'
    if not path.exists():raise FileNotFoundError('Missing saved image: '+str(path))
    args.reference=path
if args.reference:
    with args.reference.open('rb') as f:
        r=requests.post(URL+'/upload/image',files={'image':(args.reference.name,f)},data={'type':'input','overwrite':'false'},timeout=60)
    r.raise_for_status();uploaded=r.json()
    graph=image_graph(args.project)
    graph['7']['inputs']['image']='/'.join(filter(None,[uploaded.get('subfolder'),uploaded['name']]))
    if args.resume:graph['8']=node('Hy3D21LoadImageWithTransparency',image=graph['7']['inputs']['image'])
else:
    graph=text_graph(args.project)
    graph['1']['inputs']['text']=args.prompt
    graph['4']['inputs']['text']=args.negative
    graph['6']['inputs']['seed']=args.seed
graph|=shape_graph(project=args.project,resolution=cfg['resolution'],polygons=args.polygons or cfg['polygons'])
if args.resume:graph['10']['inputs']['image']=['8',2]
graph['10']['inputs']['seed']=args.seed
if args.resume=='paint':graph['10']=node('LocalAssetStudioLoadCheckpoint',project_name=args.project,filename='optimized.glb')
if args.mode=='pbr' or args.resume=='paint':
    graph=with_paint(graph,args.project,args.texture_size or cfg['texture'])
    graph['13']['inputs']['seed']=args.seed
    if args.resume:graph['13']['inputs']['image']=['8',2]
graph['11']['inputs'].update(format=args.format,height_m=args.height_m)
run(graph,'generation-'+args.project)
if args.output_directory:
    dest=args.output_directory.resolve()/args.project
    source=ROOT/'Outputs'/args.project
    if dest!=source.resolve():shutil.copytree(source,dest,dirs_exist_ok=False)
print('Saved:',ROOT/'Outputs'/args.project)
