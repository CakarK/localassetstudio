$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'AI3D\.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'AI3D\Scripts\stop.py')
if ($LASTEXITCODE -ne 0) { throw 'Unable to stop AI3D safely. See message above.' }
