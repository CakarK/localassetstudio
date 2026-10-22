"""Isolated TRELLIS backend. Removed providers never silently migrate old jobs."""
import json
from pathlib import Path
from .common import AI, ROOT, ProductionError, atomic_json, file_info

RUNTIME = AI/'TrellisRuntime'
PROVIDERS = ('trellis2',)


def provider(settings):
    value = settings.get('geometry_provider', 'trellis2')
    if value == 'hunyuan3d-2.1':
        raise ProductionError('ProviderRemoved', 'Hunyuan has been removed. Create a new TRELLIS job; old job provenance is preserved.')
    if value not in PROVIDERS:
        raise ValueError('Unknown 3D provider: '+str(value))
    return value


def ensure_unreal_closed():
    import psutil
    for process in psutil.process_iter(['pid', 'name']):
        name = (process.info['name'] or '').lower()
        if name.startswith('unrealeditor'):
            raise ProductionError('UnrealEditorOpen',
                'Unreal Editor açık (PID '+str(process.pid)+'); TRELLIS işlemi başlatılmadı. Editör kapandıktan sonra devam edin.')


def availability():
    needed = [RUNTIME/'.venv/Scripts/python.exe', RUNTIME/'web_stage.py',
              RUNTIME/'public-model-downloads.json', RUNTIME/'dino-downloads.json']
    return all(p.is_file() for p in needed)


def execute(worker, stage, attempt, settings):
    from .processes import run_process
    from .image_providers import gpu_preflight
    from .quality_vlm import check_vlm_reservation
    work = Path(attempt['work_dir'])
    if not availability():
        raise ProductionError('ProviderUnavailable', 'Isolated TRELLIS runtime or approved local models are missing; no automatic download/fallback.')
    command = [RUNTIME/'.venv/Scripts/python.exe', RUNTIME/'web_stage.py', stage, work]
    if stage == 'ReferenceProcessed':
        command += ['--reference', worker.path('ReferenceGenerated', 'reference')]
    else:
        ensure_unreal_closed()
        if not worker.allow_gpu:
            raise ProductionError('GPUApprovalRequired', 'Explicit queue/run authorization required.')
        check_vlm_reservation()
        # Never silently repeat a local inference after an interrupted process.
        for old in worker.db.attempts(attempt['job_id']):
            if old['stage_name'] == stage and (Path(old['work_dir'])/'inference-started.json').exists():
                raise ProductionError('SubmissionUnknown', 'TRELLIS attempt already started; inspect preserved files before any new inference.', uncertain=True)
        gpu_preflight(settings['comfy_url'], 19000)
        worker.boundary(attempt['job_id'])
        ensure_unreal_closed()
        command += ['--reference', worker.path('ReferenceProcessed', 'preprocessed'),
                    '--seed', str(settings['seed']), '--steps', str(settings.get('trellis_steps', 12)),
                    '--texture-size', str(settings['texture_size'])]
        if stage == 'PBRGenerated':
            command += ['--latent', worker.path('GeometryGenerated', 'shape_latent'),
                        '--optimized', worker.path('GeometryOptimized', 'optimized')]
        if stage == 'GeometryOptimized':
            command += ['--geometry',worker.path('GeometryGenerated','geometry'),'--target',str(settings['polygon_target'])]
        atomic_json(work/'inference-started.json', {'stage': stage, 'provider': 'trellis2', 'attempt_id': attempt['id']})
    run_process(command, work, max(settings['subprocess_timeout'], 900), worker.db, attempt, 'trellis')
    names = {'ReferenceProcessed': {'preprocessed':'preprocessed.png','segmentation':'segmentation.json'},
             'GeometryOptimized': {'optimized':'optimized.glb','optimization_report':'optimization.json'},
             'GeometryGenerated': {'geometry':'geometry.glb','shape_latent':'shape-latent.npz','provider_report':'provider.json'},
             'PBRGenerated': {'pbr':'pbr.glb','albedo':'albedo.png','metallic':'metallic.png','roughness':'roughness.png','provider_report':'provider.json'}}
    return {key:work/name for key,name in names[stage].items()}
