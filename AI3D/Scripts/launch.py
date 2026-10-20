"""Launch with process-scoped settings, verified TLS, and no global changes."""
from pathlib import Path
import os
import runpy
import sys

for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='backslashreplace',line_buffering=True)

ROOT=Path(__file__).resolve().parents[1]
import json
(ROOT/'Logs/server.json').write_text(json.dumps({'pid':os.getpid(),'root':str(ROOT)}),'utf-8')
os.environ['HF_HOME']=str(ROOT/'Models/huggingface')
os.environ['U2NET_HOME']=str(ROOT/'Models/rembg')
os.environ['PYTHONUTF8']='1'
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['HF_HUB_OFFLINE']='1'
os.environ['TRANSFORMERS_OFFLINE']='1'
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
os.chdir(ROOT/'ComfyUI')
sys.path.insert(0,str(ROOT/'ComfyUI'))
sys.argv=[str(ROOT/'ComfyUI/main.py'),'--listen','127.0.0.1','--port','8190','--lowvram','--reserve-vram','2',
          '--output-directory',str(ROOT/'Outputs'),'--disable-auto-launch',*sys.argv[1:]]
runpy.run_path(str(ROOT/'ComfyUI/main.py'),run_name='__main__')
