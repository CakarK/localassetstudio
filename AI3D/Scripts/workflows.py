"""Generate UI-loadable and API workflows against the live node schemas."""
import copy
import json
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
URL='http://127.0.0.1:8190'

def node(kind,**inputs): return {'class_type':kind,'inputs':inputs}

def text_graph(project='asset_001'):
    return {
      '1':node('LocalAssetStudioPrompt',text='',translate_optimize=True,project_name=project),
      '2':node('CheckpointLoaderSimple',ckpt_name='dreamshaper_8.safetensors'),
      '3':node('CLIPTextEncode',text=['1',0],clip=['2',1]),
      '4':node('CLIPTextEncode',text='cropped, multiple objects, text, watermark, people, complex background, blur, distorted perspective',clip=['2',1]),
      '5':node('EmptyLatentImage',width=512,height=512,batch_size=1),
      '6':node('KSampler',model=['2',0],positive=['3',0],negative=['4',0],latent_image=['5',0],seed=42,steps=25,cfg=7.0,sampler_name='dpmpp_2m',scheduler='karras',denoise=1.0),
      '7':node('VAEDecode',samples=['6',0],vae=['2',2]),
      '8':node('LocalAssetStudioImageCheckpoint',image=['7',0],project_name=project,remove_background=True),
      '9':node('SaveImage',images=['8',0],filename_prefix=project+'/preprocessed_view')}

def shape_graph(image=['8',0],project='asset_001',resolution=384,polygons=20000):
    return {'10':node('LocalAssetStudioShape',image=image,project_name=project,seed=42,steps=30,resolution=resolution,polygon_target=polygons,allow_oom_retry=False),
            '11':node('LocalAssetStudioExport',mesh=['10',0],project_name=project,format='glb',height_m=1.0,output_directory=''),
            '12':node('Preview3D',model_file=['11',0])}

def image_graph(project='asset_001'):
    return {'7':node('LoadImage',image='reference.png'),
            '8':node('LocalAssetStudioImageCheckpoint',image=['7',0],project_name=project,remove_background=True)}

def with_paint(graph,project='asset_001',texture=1024):
    graph=copy.deepcopy(graph)
    graph['13']=node('LocalAssetStudioPBR',mesh=['10',0],image=['8',0],project_name=project,texture_size=texture,view_size=512,steps=10,seed=42)
    graph['11']['inputs']['mesh']=['13',0]
    return graph

def to_ui(graph,schemas):
    nodes=[];links=[];lid=0
    for index,(nid,n) in enumerate(graph.items()):
        schema=schemas[n['class_type']]
        inputs=[];widgets=[]
        defs={**schema['input'].get('required',{}),**schema['input'].get('optional',{})}
        for name,spec in defs.items():
            if name not in n['inputs']:continue
            value=n['inputs'][name]; typ=spec[0];opts=spec[1] if len(spec)>1 else {}
            primitive=isinstance(typ,list) or typ in ('STRING','INT','FLOAT','BOOLEAN')
            linked=isinstance(value,list) and len(value)==2 and isinstance(value[0],str) and value[0] in graph
            if linked or not primitive:
                entry={'name':name,'type':('COMBO' if isinstance(typ,list) else typ),'link':None}
                if primitive: entry['widget']={'name':name}
                if linked:
                    lid+=1;entry['link']=lid
                    links.append([lid,int(value[0]),value[1],int(nid),len(inputs),entry['type']])
                inputs.append(entry)
            if primitive:
                default=opts.get('default',typ[0] if isinstance(typ,list) else {'STRING':'','INT':0,'FLOAT':0.0,'BOOLEAN':False}[typ])
                widgets.append(default if linked else value)
                if name in ('seed','noise_seed'):widgets.append('fixed')
        outputs=[{'name':schema.get('output_name',schema['output'])[i], 'type':t,'links':[]} for i,t in enumerate(schema['output'])]
        nodes.append({'id':int(nid),'type':n['class_type'],'pos':[index%4*440,index//4*420],
                      'size':[390,340],'flags':{},'order':index,'mode':0,'inputs':inputs,'outputs':outputs,
                      'properties':{'Node name for S&R':n['class_type']},'widgets_values':widgets})
    byid={n['id']:n for n in nodes}
    for l in links:byid[l[1]]['outputs'][l[2]]['links'].append(l[0])
    return {'last_node_id':max(byid),'last_link_id':lid,'nodes':nodes,'links':links,'groups':[],
            'config':{},'extra':{'ds':{'scale':0.7,'offset':[0,0]}},'version':0.4}

def main():
    schemas=requests.get(URL+'/object_info',timeout=60).json()
    (ROOT/'Logs/node-schemas.json').write_text(json.dumps(schemas),encoding='utf-8')
    workflows={'01_Text_to_3D':text_graph()|shape_graph(),
               '02_Image_to_3D':image_graph()|shape_graph(),
               '03_Image_to_3D_PBR':with_paint(image_graph()|shape_graph()),
               '04_Low_Poly_Game_Asset':text_graph()|shape_graph(resolution=256,polygons=5000),
               '05_High_Quality_Game_Asset':with_paint(text_graph()|shape_graph(resolution=384,polygons=80000),texture=2048),
               '06_Text_to_Image':text_graph(),
               '07_Resume_PBR':with_paint(image_graph()|{'10':node('LocalAssetStudioLoadCheckpoint',project_name='asset_001',filename='optimized.glb'),
                    '11':node('LocalAssetStudioExport',mesh=['10',0],project_name='asset_001',format='glb',height_m=1.0),
                    '12':node('Preview3D',model_file=['11',0])})}
    user=ROOT/'ComfyUI/user/default/workflows/LocalAssetStudio'
    user.mkdir(parents=True,exist_ok=True)
    for name,g in workflows.items():
        for n in g.values():
            if n['class_type'] not in schemas:raise RuntimeError('Missing node '+n['class_type'])
        ui=to_ui(g,schemas)
        (ROOT/'Workflows'/f'{name}.json').write_text(json.dumps(ui,ensure_ascii=False,indent=2),'utf-8')
        (ROOT/'Workflows'/f'{name}.api.json').write_text(json.dumps(g,ensure_ascii=False,indent=2),'utf-8')
        (user/f'{name}.json').write_text(json.dumps(ui,ensure_ascii=False,indent=2),'utf-8')
    print('Generated',len(workflows),'UI and API workflows')

if __name__=='__main__': main()
