$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot 'AI3D\.venv\Scripts\python.exe'
Push-Location $PSScriptRoot
try {
@'
import psutil
from pathlib import Path
root=Path.cwd().resolve();record=root/'AI3D/State/web-server.pid'
ident=int(record.read_text()) if record.exists() else None
if ident and psutil.pid_exists(ident):
    process=psutil.Process(ident)
    for child in reversed([process]+process.children(recursive=True)):
        try:
            if 'ai3d_manager.web_app' in child.cmdline() and Path(child.cwd()).resolve()==root:
                child.terminate();child.wait(timeout=10)
        except psutil.NoSuchProcess:pass
'@ | & $pythonPath -
} finally { Pop-Location }
Write-Host 'Web sunucusu durduruldu. Devam eden üretim işlemleri ayrı süreçte korunur.'
