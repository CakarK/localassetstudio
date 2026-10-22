param([switch]$Plan, [switch]$NoStart, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$ai = Join-Path $root 'AI3D'
$lock = Get-Content -LiteralPath (Join-Path $ai 'install.lock.json') -Raw | ConvertFrom-Json
if ($Plan) {
    Write-Host 'Local Asset Studio full Windows installation (no changes or downloads)'
    Write-Host "Python $($lock.python); core torch 2.6/CUDA 12.6; image torch 2.10/CUDA 13.0"
    Write-Host 'TRELLIS.2 / DINOv3 / BiRefNet + DreamShaper 8 + Qwen-Image-2512 + Z-Image-Turbo'
    Write-Host 'DINOv3 requires free approved Hugging Face access. Native wheels: Windows CPython 3.12, RTX 3090 (sm_86).'
    Write-Host 'Portable uv, Git, Blender and Ollama; qwen3:4b and qwen3.6:27b'
    Write-Host "Model downloads: $([math]::Round($lock.model_download_bytes / 1GB, 2)) GiB, plus Python packages and tools."
    Write-Host "Requirements: 24 GB NVIDIA VRAM; $($lock.minimum_free_disk_gib) GiB free disk; Windows 11 x64."
    Write-Host 'Upstream license links: THIRD_PARTY_NOTICES.md. No inference is run by setup.'
    return
}
. (Join-Path $ai 'Scripts/install_functions.ps1')
Test-LASHardware $lock $root
$gpuArchitecture=@(& nvidia-smi --query-gpu=compute_cap --format=csv,noheader)[0].Trim()
if ($LASTEXITCODE -ne 0 -or $gpuArchitecture -ne '8.6') { throw 'Bundled native wheels require GPU 0 compute capability 8.6 (RTX 3090 24 GB). See docs/TRELLIS_MIGRATION.md for native builds on other architectures.' }
$logDir = Join-Path $ai 'Logs/Install'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$transcript = Join-Path $logDir ('setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
Start-Transcript -LiteralPath $transcript | Out-Null
$setupLock=$null;$productionLock=$null
try {
    $setupLockPath = Join-Path $ai 'State/install.lock'
    New-Item -ItemType Directory -Force -Path (Split-Path $setupLockPath) | Out-Null
    try { $setupLock = [IO.File]::Open($setupLockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another installer owns this installation. No concurrent setup allowed.' }
    try { $productionLock = [IO.File]::Open((Join-Path $ai 'State/production.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Production owns this installation. Finish or pause known work before setup; no lock was removed.' }
    foreach ($port in @(8190,8191)) {
        $listening = $false
        try { $null = Invoke-RestMethod "http://127.0.0.1:$port/system_stats" -TimeoutSec 2; $listening=$true } catch {}
        if ($listening) { throw "ComfyUI is already running on port $port. Stop it yourself before changing environments, then rerun setup. No process was stopped." }
    }
    Write-Host 'Local Asset Studio full setup. Rerun this command to resume after an interruption.'
    Write-Host $lock.license_notice
    Write-Host "Model download size: $([math]::Round($lock.model_download_bytes / 1GB,2)) GiB; packages and tools are additional."
    $tools = Join-Path $ai 'Tools'; $cache = Join-Path $ai '.cache/install'
    $uv = Install-LASTool 'uv' $lock.tools.uv $tools $cache
    $git = Install-LASTool 'git' $lock.tools.git $tools $cache
    $blender = Install-LASTool 'blender' $lock.tools.blender $tools $cache
    $ollama = Install-LASTool 'ollama' $lock.tools.ollama $tools $cache
    $env:PATH = (Split-Path $uv) + ';' + (Split-Path $git) + ';' + $env:PATH
    # Cache and managed Python stay on the selected installation drive.
    $env:UV_CACHE_DIR = Join-Path $ai '.cache/uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
    $env:UV_NATIVE_TLS = 'true'
    $env:PYTHONUTF8 = '1'
    Write-Host '[1/6] Python and core image/application environment'
    & (Join-Path $ai 'Scripts/setup.ps1') -Python312 $lock.python -SkipModels
    $python = Join-Path $ai '.venv/Scripts/python.exe'
    Write-Host 'Checking free DINOv3 account access before large model downloads'
    Invoke-LASCommand $python @((Join-Path $ai 'TrellisRuntime/check_access.py'))
    Write-Host '[2/6] Separate native image ComfyUI environment'
    $image = Join-Path $ai 'ImageComfyUI'
    Install-LASSource $git $lock.image_comfy $image
    $imagePython = Join-Path $image '.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $imagePython)) { Invoke-LASCommand $uv @('venv','--python',$lock.python,(Join-Path $image '.venv')) }
    Invoke-LASCommand $uv @('pip','install','--python',$imagePython,'-r',(Join-Path $ai 'image-requirements.lock.txt'),'--extra-index-url','https://download.pytorch.org/whl/cu130','--index-strategy','unsafe-best-match')
    Write-Host '[3/6] Isolated TRELLIS runtime and verified native wheels'
    $trellis=Join-Path $ai 'TrellisRuntime'
    $trellisPython=Join-Path $trellis '.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $trellisPython)) { Invoke-LASCommand $uv @('venv','--python',$lock.python,(Join-Path $trellis '.venv')) }
    Invoke-LASCommand $uv @('pip','install','--python',$trellisPython,'-r',(Join-Path $trellis 'requirements.resolved.txt'),'--extra-index-url','https://download.pytorch.org/whl/cu130','--index-strategy','unsafe-best-match')
    $native=Get-Content -LiteralPath (Join-Path $trellis 'wheels/manifest.json') -Raw | ConvertFrom-Json
    foreach ($wheel in $native.files) {
        if ([IO.Path]::GetFileName($wheel.file) -ne $wheel.file -or !$wheel.file.EndsWith('.whl')) { throw 'Invalid wheel filename' }
        $wheelPath=Join-Path $trellis ('wheels/'+$wheel.file)
        if ((Get-LASFileSHA256 $wheelPath) -ne $wheel.sha256 -or (Get-Item -LiteralPath $wheelPath).Length -ne $wheel.size) { throw 'Native wheel integrity failure' }
        Invoke-LASCommand $uv @('pip','install','--python',$trellisPython,'--no-deps',$wheelPath)
    }
    Write-Host '[4/6] Verified model files (resumable)'
    Invoke-LASCommand $python @((Join-Path $ai 'Scripts/install_models.py'))
    Invoke-LASCommand $trellisPython @((Join-Path $trellis 'download_models.py'))
    Invoke-LASCommand $trellisPython @((Join-Path $trellis 'download_models.py'),'--dino')
    Write-Host '[5/6] Local runtime paths and Ollama models'
    $runtime = @{version=1; blender=$blender; image_source=$image; image_python=$imagePython; ollama=$ollama; ollama_models=(Join-Path $ai 'Models/ollama')}
    New-Item -ItemType Directory -Path (Join-Path $ai 'State') -Force | Out-Null
    $runtime | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ai 'State/install-runtime.json') -Encoding UTF8
    & (Join-Path $root 'Start-Ollama.ps1')
    Invoke-LASCommand $python @((Join-Path $ai 'Scripts/install_models.py'),'--ollama-only')
    Write-Host '[6/6] CUDA, extensions, Blender and installed-file checks'
    Invoke-LASCommand $python @((Join-Path $ai 'Scripts/verify_install.py'),'--image-python',$imagePython,'--blender',$blender)
    if (!$NoStart) {
        & (Join-Path $root 'Start-LocalAssetStudio.ps1') -NoBrowser:$NoBrowser
        Invoke-LASCommand $python @((Join-Path $ai 'Scripts/verify_install.py'),'--services-only')
    }
    Write-Host 'Installation checks completed. No image or 3D inference was submitted.' -ForegroundColor Green
    Write-Host "Start later with: powershell -ExecutionPolicy Bypass -File `"$root\Start-LocalAssetStudio.ps1`""
} finally {
    if (Get-Variable productionLock -ErrorAction SilentlyContinue) { if ($productionLock) { $productionLock.Dispose() } }
    if (Get-Variable setupLock -ErrorAction SilentlyContinue) { if ($setupLock) { $setupLock.Dispose() } }
    Stop-Transcript | Out-Null
}
