param([switch]$NoBrowser,[string]$ComfyUISource=$env:LAS_IMAGE_COMFY_SOURCE,[string]$Python=$env:LAS_IMAGE_PYTHON)
$ErrorActionPreference='Stop'
$taskRoot=Join-Path $PSScriptRoot 'AI3D'
if (!$ComfyUISource) { $ComfyUISource=Join-Path $taskRoot 'ImageComfyUI' }
if (!$Python) { $Python=Join-Path $ComfyUISource '.venv\Scripts\python.exe' }
$sourceMain=Join-Path $ComfyUISource 'main.py'
if (!(Test-Path -LiteralPath $Python) -or !(Test-Path -LiteralPath $sourceMain)) { throw 'Configure LAS_IMAGE_COMFY_SOURCE and LAS_IMAGE_PYTHON. See docs/INSTALLATION.md. No packages are installed by this launcher.' }
$env:LAS_IMAGE_COMFY_SOURCE=(Resolve-Path -LiteralPath $ComfyUISource).Path
$logRoot=Join-Path $taskRoot 'Logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$stats=$null
try { $stats=Invoke-RestMethod 'http://127.0.0.1:8191/system_stats' -TimeoutSec 3 } catch {}
if (!$stats) {
    $scriptPath=Join-Path $taskRoot 'Scripts\launch_image_runtime.py'
    $process=Start-Process -FilePath $Python -ArgumentList ('"'+$scriptPath+'"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'image-runtime.stdout.log') -RedirectStandardError (Join-Path $logRoot 'image-runtime.stderr.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $logRoot 'image-runtime.pid')
    for ($attempt=0; $attempt -lt 90; $attempt++) {
        if ($process.HasExited) { throw 'Image runtime stopped; inspect AI3D/Logs/image-runtime.stderr.log.' }
        try { $stats=Invoke-RestMethod 'http://127.0.0.1:8191/system_stats' -TimeoutSec 2; break } catch { Start-Sleep -Seconds 1 }
    }
    if (!$stats) { throw 'Image runtime startup timed out; no model inference was submitted.' }
}
if ([System.IO.Path]::GetFullPath($stats.system.argv[0]) -ne [System.IO.Path]::GetFullPath($sourceMain)) { throw 'Port 8191 belongs to another installation; no process was changed.' }
Write-Host 'Local Asset Studio image runtime ready: http://127.0.0.1:8191'
if (!$NoBrowser) { Start-Process 'http://127.0.0.1:8191' }
