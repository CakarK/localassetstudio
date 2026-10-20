"""Real CUDA calculation, allocation/release, server and installed package report."""
import gc
import importlib.metadata as meta
import json
import platform
import subprocess
from pathlib import Path
import psutil
import requests
import torch

ROOT=Path(__file__).resolve().parents[1]
prop=torch.cuda.get_device_properties(0)
# Initialize cuBLAS once: its persistent workspace is not a leaked model tensor.
warm=torch.ones((32,32),device='cuda');warm_result=warm@warm
torch.cuda.synchronize()
del warm,warm_result
gc.collect()
torch.cuda.empty_cache()
before=torch.cuda.memory_allocated()
a=torch.ones((2048,2048),device='cuda')
result=(a@a)[0,0].item()
peak=torch.cuda.memory_allocated()
del a;gc.collect();torch.cuda.empty_cache()
after=torch.cuda.memory_allocated()
r=requests.get('http://127.0.0.1:8190/system_stats',timeout=10);r.raise_for_status()
report={'os':platform.platform(),'python':platform.python_version(),'torch':torch.__version__,
    'cuda_runtime':torch.version.cuda,'gpu':prop.name,'vram_bytes':prop.total_memory,
    'ram_bytes':psutil.virtual_memory().total,'cuda_calculation_pass':result==2048,
    'memory_before':before,'memory_during':peak,'memory_after':after,'allocation_cleanup_pass':after==before,
    'server':r.json(),'nvidia_smi':subprocess.check_output(['nvidia-smi'],text=True),
    'packages':{d.metadata['Name']:d.version for d in meta.distributions()}}
(ROOT/'Logs/environment.json').write_text(json.dumps(report,indent=2),'utf-8')
assert report['cuda_calculation_pass'] and report['allocation_cleanup_pass']
print('CUDA_CALCULATION_PASS; ALLOCATION_CLEANUP_PASS;',prop.name,prop.total_memory)
