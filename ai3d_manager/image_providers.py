"""Pinned image workflows. Legacy DreamShaper graph remains unchanged."""
import json
import shutil
import time
from pathlib import Path
import requests
from .common import AI, ProductionError, atomic_json, digest, file_info
from .comfy import ComfyClient, graphs
from .quality_vlm import check_vlm_reservation, gpu_sample, GPUQualityLease

CONFIG = AI/'Config/image_providers.json'


def load_registry(path=None):
    value = json.loads(Path(path or CONFIG).read_text('utf-8'))
    if value['default_provider'] not in value['providers']: raise ValueError('Unknown default image provider')
    for ident, p in value['providers'].items():
        if p['id'] != ident or p['kind'] not in ('dreamshaper', 'qwen', 'zimage', 'flux', 'sd35'): raise ValueError('Invalid provider definition')
        if not p['revision'] or not p['workflow_id'] or type(p['negative_prompt']) is not bool: raise ValueError('Incomplete provider metadata')
        if p['width'] % 16 or p['height'] % 16 or p['steps'] < 1: raise ValueError('Invalid image generation settings')
        if p['kind']=='dreamshaper' and p['width']!=p['height']:raise ValueError('Legacy DreamShaper provider supports square presets only')
        for f in p['files']:
            if len(f['sha256']) != 64: raise ValueError('Pinned SHA256 required')
    return value


def provider_id(asset, settings, registry=None):
    r = registry or load_registry()
    if asset.get('phase33_set_id'):
        if (settings.get('image_provider') or asset.get('image_provider')) not in (None,'dreamshaper8'):raise ProductionError('ProviderConfigurationMismatch','Phase3.3 campaign keeps its original DreamShaper workflow')
        return 'dreamshaper8'
    categories=r.get('category_overrides',{});category_provider=None
    if categories:
        from .gate_analysis import profile_for
        semantic={'shelter':'Buildings','building':'Buildings','tower':'Towers','tree':'Organic','debris':'Debris'}.get(profile_for(asset)['name'])
        category_provider=categories.get(semantic) or categories.get(asset.get('category'))
    from .category_pipeline import active_provider
    category_default=active_provider(asset,settings)
    ident = settings.get('image_provider') or asset.get('image_provider') or r.get('asset_overrides', {}).get(asset.get('asset_id')) or category_provider or category_default or r['default_provider']
    if ident not in r['providers']: raise ValueError('Unknown image provider: '+ident)
    return ident


def uses_provider(asset,settings):
    if asset.get('phase33_set_id'):return False
    return bool(asset.get('image_provider') or settings.get('image_provider') or provider_id(asset,settings)!='dreamshaper8')


class DualComfyQualityLease(GPUQualityLease):
    """Existing lease and reservation; additionally reject busy image instance."""
    def queue_empty(self):
        queue=super().queue_empty()
        response=self.vlm.session.get('http://127.0.0.1:8191/queue',timeout=10);response.raise_for_status();other=response.json()
        if other.get('queue_running') or other.get('queue_pending'):raise ProductionError('GPUBusy','Image provider instance is busy; no Qwen inference/unload')
        return queue


class ImageGenerationProvider:
    def __init__(self, ident, registry=None):
        self.registry = registry or load_registry(); self.spec = self.registry['providers'][ident]

    def verify(self, nodes=None, cache_root=None):
        p = self.spec; reports = []; root = Path(cache_root or AI/'State/ImageModelVerification')
        root.mkdir(parents=True, exist_ok=True)
        for spec in p['files']:
            path = Path(spec['path']); result = {**spec, 'status': 'Missing'}
            if path.is_file():
                st = path.stat(); signature = {'path': str(path.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'expected': spec['sha256']}
                cache = root/(digest(signature)+'.json')
                info = json.loads(cache.read_text('utf-8')) if cache.is_file() else file_info(path)
                if not cache.is_file(): atomic_json(cache, info)
                result.update(actual_sha256=info['sha256'], status='PASS' if st.st_size == spec['size'] and info['sha256'] == spec['sha256'] else 'HashMismatch')
            reports.append(result)
        status = 'PASS' if reports and all(r['status'] == 'PASS' for r in reports) else 'BLOCKED'
        if nodes is not None:
            try: validate_graph(self.graph('verification', {'optimized': 'test', 'negative': 'test'}, 1), nodes)
            except (ValueError, KeyError) as e: return {'provider_id': p['id'], 'status': 'BLOCKED', 'files': reports, 'error': str(e)}
        return {'provider_id': p['id'], 'status': status, 'files': reports, 'revision': p['revision'], 'quantization': p['quantization']}

    def graph(self, namespace, prompt, seed):
        p = self.spec
        def node(kind, **inputs): return {'class_type': kind, 'inputs': inputs}
        if p['kind'] == 'dreamshaper':
            g = graphs('ReferenceGenerated', namespace, {'checkpoint': p['checkpoint'], 'image_size': p['width'], 'seed': seed, 'image_steps': p['steps']}, prompt)
            g['5']['inputs'].update(cfg=p['guidance'], sampler_name=p['sampler'], scheduler=p['scheduler'])
            return g
        if p['kind'] not in ('qwen', 'zimage'): raise ProductionError('ProviderUnavailable', 'Research-only provider; no verified local workflow')
        g = {'1': node('UNETLoader', unet_name=p['diffusion_model'], weight_dtype='default'),
             '2': node('CLIPLoader', clip_name=p['text_encoder'], type='qwen_image' if p['kind'] == 'qwen' else 'lumina2', device='default'),
             '3': node('VAELoader', vae_name=p['vae']), '4': node('CLIPTextEncode', clip=['2', 0], text=prompt['optimized']),
             '5': node('EmptySD3LatentImage', width=p['width'], height=p['height'], batch_size=1),
             '6': node('ModelSamplingAuraFlow', model=['1', 0], shift=p['shift'])}
        if p['negative_prompt']:
            g['7'] = node('CLIPTextEncode', clip=['2', 0], text=prompt['negative'])
        else: g['7'] = node('ConditioningZeroOut', conditioning=['4', 0])
        model = ['6', 0]
        if p['kind'] == 'qwen':
            g['8'] = node('CFGNorm', model=model, strength=1.0); model = ['8', 0]
        g['9'] = node('KSampler', model=model, positive=['4', 0], negative=['7', 0], latent_image=['5', 0], seed=seed, steps=p['steps'], cfg=p['guidance'], sampler_name=p['sampler'], scheduler=p['scheduler'], denoise=1.)
        g['10'] = node('VAEDecode', samples=['9', 0], vae=['3', 0])
        g['11'] = node('SaveImage', images=['10', 0], filename_prefix=namespace+'/reference')
        return g

    def generate(self, worker, attempt, asset, settings):
        p = self.spec; work = Path(attempt['work_dir']); client = ComfyClient(p['comfy_url'], settings['comfy_timeout'])
        context = json.loads(attempt.get('context_json') or '{}')
        if asset.get('phase34_benchmark_id'):
            from .image_benchmark import guard_submission
            guard_submission(worker.db, asset, attempt, 'reference', p)
        else: gpu_preflight(p['comfy_url'], p['minimum_free_mib'])
        if context: graph = None; namespace = context['namespace']
        else:
            verification = self.verify()
            if verification['status'] != 'PASS': raise ProductionError('ImageModelUnavailable', json.dumps(verification))
            prompt = json.loads(worker.path('PromptPrepared', 'prompt').read_text('utf-8')); namespace = 'GM_'+attempt['id']
            graph = self.graph(namespace, prompt, settings['seed']); nodes = client.get('/object_info'); validate_graph(graph, nodes)
            stats = client.get('/system_stats'); atomic_json(work/'image-provider.json', {**p, 'verification': verification, 'seed': settings['seed'], 'runtime': stats['system'], 'negative_applied': p['negative_prompt']})
            context = {'namespace': namespace, 'stage': 'ReferenceGenerated', 'provider_id': p['id']}
        started = time.monotonic()
        history = client.execute(worker.db, attempt, graph, context, lambda: worker.boundary(attempt['job_id']))
        images = [i for r in history.get('outputs', {}).values() for i in r.get('images', [])]
        if len(images) != 1: raise ProductionError('ImageInference', 'Expected exactly one reference output')
        info = images[0]; source = (Path(p['output_root'])/info.get('subfolder', '')/info['filename']).resolve()
        if not source.is_relative_to((Path(p['output_root'])/namespace).resolve()): raise ProductionError('ExportFailure', 'Output escaped owned namespace')
        shutil.copyfile(source, work/'reference.png')
        events=history.get('status',{}).get('messages',[])
        begin=next((v.get('timestamp') for k,v in events if k=='execution_start'),None)
        end=next((v.get('timestamp') for k,v in events if k=='execution_success'),None)
        remote_seconds=(end-begin)/1000 if isinstance(begin,(int,float)) and isinstance(end,(int,float)) and end>=begin else None
        atomic_json(work/'image-inference.json', {'elapsed_seconds': remote_seconds if remote_seconds is not None else time.monotonic()-started,
            'timing_source':'Comfy execution_start/success' if remote_seconds is not None else 'client wait monotonic',
            'recovered_remote_attempt':bool(json.loads(attempt.get('context_json') or '{}')),
            'observed_wait_seconds':time.monotonic()-started,'image': file_info(work/'reference.png'), 'provider_id': p['id']})
        # Only release this finished instance's idle model, with no foreign queue.
        queue = client.get('/queue')
        if not queue.get('queue_running') and not queue.get('queue_pending'):
            r = requests.post(p['comfy_url']+'/free', json={'unload_models': True, 'free_memory': True}, timeout=20); r.raise_for_status()
        return {'reference': work/'reference.png', 'image_provider': work/'image-provider.json', 'image_inference': work/'image-inference.json'}


def validate_graph(graph, nodes):
    for ident, node in graph.items():
        if node['class_type'] not in nodes: raise ValueError('Missing node '+node['class_type'])
        schema = nodes[node['class_type']]['input']; required = schema.get('required', {}); optional = schema.get('optional', {})
        if set(required)-set(node['inputs']): raise ValueError('Missing required input '+ident)
        if set(node['inputs'])-set(required)-set(optional): raise ValueError('Unknown workflow input '+ident)
        for key, value in node['inputs'].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in graph:
                if value[1] >= len(nodes[graph[value[0]]['class_type']]['output']): raise ValueError('Invalid graph output link')
            elif isinstance((required | optional)[key][0], list) and value not in (required | optional)[key][0]: raise ValueError('Unsupported workflow value '+key+': '+str(value))
    return True


def wait_for_ollama_idle(polls=17, interval=.5):
    """keep_alive=0 can return before the translation runner leaves /api/ps.

    Residency alone does not identify another request. Allow a bounded handoff,
    never unload a model and never proceed while any model remains resident.
    """
    for number in range(polls):
        check_vlm_reservation()
        ps=requests.get('http://127.0.0.1:11434/api/ps',timeout=10);ps.raise_for_status()
        resident=ps.json().get('models',[])
        if not resident:return
        if number+1<polls:time.sleep(interval)
    names=', '.join(str(m.get('name') or m.get('model') or 'unknown') for m in resident)
    raise ProductionError('GPUBusy','Ollama modeli hâlâ bellekte ('+names+'). Kısa bekleme sonunda boşalmadı; etkin modele dokunulmadı. İşlem tamamlandıktan sonra aynı kayıttan yeniden deneyin.',transient=True)


def gpu_preflight(url, minimum=12000):
    wait_for_ollama_idle()
    endpoints=list(dict.fromkeys(['http://127.0.0.1:8190', 'http://127.0.0.1:8191', url]))
    for endpoint in endpoints:
        q = requests.get(endpoint+'/queue', timeout=10); q.raise_for_status(); queue = q.json()
        if queue.get('queue_running') or queue.get('queue_pending'): raise ProductionError('GPUBusy', 'Foreign Comfy work preserved')
    for endpoint in endpoints:
        r = requests.post(endpoint+'/free', json={'unload_models': True, 'free_memory': True}, timeout=20); r.raise_for_status()
    for _ in range(8):
        sample = gpu_sample()
        if sample['free_mib'] >= minimum: return sample
        time.sleep(.5)
    raise ProductionError('InsufficientVRAM', 'Provider requires '+str(minimum)+' MiB free; no silent quality reduction')
