param([string[]]$Packages = @('FlexGEMM', 'CuMesh', 'TRELLIS.2\o-voxel'), [string]$CudaArchitectures='8.6')
$ErrorActionPreference = 'Stop'
$runtimeRoot = $PSScriptRoot
$pythonExe = Join-Path $runtimeRoot '.venv\Scripts\python.exe'
$env:UV_CACHE_DIR = Join-Path $runtimeRoot 'cache\uv'
$env:UV_LINK_MODE = 'copy'
$env:TEMP = Join-Path $runtimeRoot 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$vsWhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$vsInstall = & $vsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsInstall) { throw 'MSVC C++ tools not found' }
Import-Module (Join-Path $vsInstall 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vsInstall -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
$env:CUDA_HOME = Split-Path (Split-Path (Get-Command nvcc.exe).Source)
$env:TORCH_CUDA_ARCH_LIST = $CudaArchitectures
$env:MAX_JOBS = '4'
$env:DISTUTILS_USE_SDK = '1'
$env:MSSdk = '1'
$env:CL = '/Zc:preprocessor'
$env:NVCC_APPEND_FLAGS = '-Xcompiler=/Zc:preprocessor'
$env:FLEX_GEMM_AUTOTUNE_CACHE_PATH = Join-Path $runtimeRoot 'cache\flex_gemm\autotune_cache.json'
$env:PYTHONUTF8 = '1'
foreach ($package in $Packages) {
    $sourcePath = Join-Path $runtimeRoot "src\$package"
    $logDir = Join-Path $runtimeRoot 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $buildLog = Join-Path $logDir (($package -replace '[\\/]', '_') + '-build.log')
    Write-Output "Building audited source: $package"
    & uv pip install --python $pythonExe --no-build-isolation --no-deps $sourcePath *> $buildLog
    if ($LASTEXITCODE -ne 0) {
        Get-Content -LiteralPath $buildLog | Select-String -Pattern 'error|fatal|FAILED' -Context 1,2 | Select-Object -First 12
        throw "Native build failed: $package; full log: $buildLog"
    }
    Get-Content -LiteralPath $buildLog -Tail 4
}
