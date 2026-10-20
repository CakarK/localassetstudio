param([string]$Python312='3.12.11')
$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot -Parent
Set-Location $taskRoot
foreach ($folder in @('Logs','State','Models','Outputs','Quality')) { New-Item -ItemType Directory -Force -Path (Join-Path $taskRoot $folder) | Out-Null }
function Invoke-Checked([scriptblock]$Action) { & $Action; if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE)." } }
$sources=Get-Content sources.lock.json -Raw | ConvertFrom-Json
if (!(Test-Path ComfyUI)) { Invoke-Checked { git clone $sources.comfyui.url ComfyUI }; Invoke-Checked { git -C ComfyUI checkout --detach $sources.comfyui.revision } }
if ((git -C ComfyUI rev-parse HEAD).Trim() -ne $sources.comfyui.revision) { throw 'Unexpected ComfyUI revision; no automatic reset performed.' }
$wrapper='ComfyUI/custom_nodes/ComfyUI-Hunyuan3d-2-1'
if (!(Test-Path $wrapper)) { Invoke-Checked { git clone $sources.wrapper.url $wrapper }; Invoke-Checked { git -C $wrapper checkout --detach $sources.wrapper.revision } }
if ((git -C $wrapper rev-parse HEAD).Trim() -ne $sources.wrapper.revision) { throw 'Unexpected wrapper revision; no automatic reset performed.' }
if (!(Test-Path .venv\Scripts\python.exe)) { Invoke-Checked { uv venv --python $Python312 .venv } }
$python=Join-Path $taskRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force .cache | Out-Null
Get-Content requirements.lock.txt | Where-Object { $_ -notmatch '^(custom[-_]rasterizer|mesh[-_]inpaint[-_]processor)==' } | Set-Content .cache\install-requirements.txt
Invoke-Checked { uv pip install --python $python -r .cache/install-requirements.txt --extra-index-url https://download.pytorch.org/whl/cu126 }
Invoke-Checked { uv pip install --python $python --no-deps "$wrapper/hy3dpaint/custom_rasterizer/dist/custom_rasterizer-0.1-cp312-cp312-win_amd64.whl" "$wrapper/hy3dpaint/DifferentiableRenderer/dist/mesh_inpaint_processor-0.0.0-cp312-cp312-win_amd64.whl" }
Invoke-Checked { & $python Scripts\apply_local_config.py }
Invoke-Checked { & $python Scripts\download_models.py }
Write-Host 'Setup complete. Run Start-AI3D.bat from the project root.'
