"""Stop only the process whose saved identity and command match this installation."""
import json
from pathlib import Path
import psutil
ROOT=Path(__file__).resolve().parents[1]
identity=ROOT/'Logs/server.json'
if not identity.exists():raise SystemExit('No server identity found. Close the AI3D process manually.')
data=json.loads(identity.read_text('utf-8'))
if Path(data['root']).resolve()!=ROOT:raise SystemExit('Installation identity mismatch.')
try:
    process=psutil.Process(data['pid'])
    target=ROOT/'Scripts/launch.py'
    if not any(Path(arg).resolve()==target for arg in process.cmdline()):raise SystemExit('PID reused; refusing to stop another process.')
    process.terminate();process.wait(timeout=15)
    print('AI3D stopped.')
except psutil.NoSuchProcess:print('AI3D is already stopped.')
