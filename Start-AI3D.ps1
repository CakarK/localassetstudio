param([switch]$NoBrowser)
$ErrorActionPreference='Stop'
$taskRoot=Join-Path $PSScriptRoot 'AI3D'
$python=Join-Path $taskRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'AI3D Python environment is missing. See INSTALLATION.md.' }
try {
    $stats=Invoke-RestMethod 'http://127.0.0.1:8190/system_stats' -TimeoutSec 3
    Write-Host 'ComfyUI is already listening on port 8190.'
} catch {
    $process=Start-Process -FilePath $python -ArgumentList ('"'+(Join-Path $taskRoot 'Scripts\launch.py')+'"') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRoot 'Logs\comfyui.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'Logs\comfyui.stderr.log')
    $process.Id | Set-Content (Join-Path $taskRoot 'Logs\comfyui.pid')
    $ready=$false
    for ($i=0; $i -lt 90; $i++) {
        if ($process.HasExited) { throw 'ComfyUI stopped. See AI3D\Logs\comfyui.stderr.log.' }
        try { $stats=Invoke-RestMethod 'http://127.0.0.1:8190/system_stats' -TimeoutSec 2; $ready=$true; break } catch { Start-Sleep -Seconds 1 }
    }
    if (!$ready) { throw 'Startup timed out; inspect AI3D\Logs.' }
}
if ($stats.system.argv[0] -ne (Join-Path $taskRoot 'ComfyUI\main.py')) { throw 'Port 8190 belongs to another installation. No process was changed.' }
if (!$NoBrowser) { Start-Process 'http://127.0.0.1:8190' }
Write-Host 'AI3D ready: http://127.0.0.1:8190'
