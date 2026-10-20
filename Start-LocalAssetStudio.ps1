param([switch]$NoBrowser)
$ErrorActionPreference='Stop'
$runtimeFile=Join-Path $PSScriptRoot 'AI3D/State/install-runtime.json'
if (!(Test-Path -LiteralPath $runtimeFile)) { throw 'Run Install-LocalAssetStudio.ps1 first.' }
$runtime=Get-Content -LiteralPath $runtimeFile -Raw | ConvertFrom-Json
$env:LAS_BLENDER_PATH=$runtime.blender
$env:LAS_IMAGE_COMFY_SOURCE=$runtime.image_source
$env:LAS_IMAGE_PYTHON=$runtime.image_python
& (Join-Path $PSScriptRoot 'Start-Ollama.ps1')
& (Join-Path $PSScriptRoot 'Start-AI3D.ps1') -NoBrowser
& (Join-Path $PSScriptRoot 'Start-ImageProviders.ps1') -NoBrowser
& (Join-Path $PSScriptRoot 'Start-AssetStudio.ps1') -NoBrowser:$NoBrowser
