param([string]$Python312='3.12.11',[switch]$SkipModels)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot -Parent
Set-Location $taskRoot
foreach ($folder in @('Logs','State','Models','Outputs','Quality')) { New-Item -ItemType Directory -Force -Path (Join-Path $taskRoot $folder) | Out-Null }
. (Join-Path $PSScriptRoot 'install_functions.ps1')
function Invoke-Checked([scriptblock]$Action) { & $Action; if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE)." } }
$sources=Get-Content sources.lock.json -Raw | ConvertFrom-Json
$git=(Get-Command git.exe).Source
Install-LASSource $git $sources.comfyui (Join-Path $taskRoot 'ComfyUI')
if (!(Test-Path .venv\Scripts\python.exe)) { Invoke-Checked { uv venv --python $Python312 .venv } }
$python=Join-Path $taskRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force .cache | Out-Null
Invoke-Checked { uv pip install --python $python -r requirements.lock.txt --extra-index-url https://download.pytorch.org/whl/cu126 --index-strategy unsafe-best-match }
Invoke-Checked { & $python Scripts\apply_local_config.py }
if (!$SkipModels) { Invoke-Checked { & $python Scripts\install_models.py } }
Write-Host 'Setup complete. Run Start-AI3D.bat from the project root.'
