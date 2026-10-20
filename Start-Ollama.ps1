$ErrorActionPreference='Stop'
$runtimeFile=Join-Path $PSScriptRoot 'AI3D/State/install-runtime.json'
if (!(Test-Path -LiteralPath $runtimeFile)) { throw 'Run Install-LocalAssetStudio.ps1 first.' }
$runtime=Get-Content -LiteralPath $runtimeFile -Raw | ConvertFrom-Json
try { $version=Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 3; return } catch {}
$env:OLLAMA_HOST='127.0.0.1:11434';$env:OLLAMA_MODELS=$runtime.ollama_models
$env:OLLAMA_MAX_LOADED_MODELS='1';$env:OLLAMA_NUM_PARALLEL='1'
$logs=Join-Path $PSScriptRoot 'AI3D/Logs';New-Item -ItemType Directory -Force -Path $logs | Out-Null
$process=Start-Process -FilePath $runtime.ollama -ArgumentList 'serve' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'ollama.stdout.log') -RedirectStandardError (Join-Path $logs 'ollama.stderr.log')
$process.Id | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'AI3D/State/ollama.pid')
for ($attempt=0;$attempt -lt 60;$attempt++) {
    if ($process.HasExited) { throw 'Ollama stopped; inspect AI3D/Logs/ollama.stderr.log.' }
    try { $version=Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2; Write-Host 'Ollama ready on localhost.'; return } catch { Start-Sleep -Seconds 1 }
}
throw 'Ollama startup timed out; no inference was submitted.'
