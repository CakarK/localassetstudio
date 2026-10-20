# Windows PowerShell 5.1 bootstrap. No Git/Python or administrator required.
param([string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'LocalAssetStudio'), [switch]$NoStart)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or ![Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell on Windows 11.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$windowsModules=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/Modules'
if (($env:PSModulePath -split ';') -notcontains $windowsModules) { $env:PSModulePath += ';' + $windowsModules }
$target = [IO.Path]::GetFullPath($InstallDirectory)
$entry = Join-Path $target 'Install-LocalAssetStudio.ps1'
if (!(Test-Path -LiteralPath $entry)) {
    if ((Test-Path -LiteralPath $target) -and @(Get-ChildItem -LiteralPath $target -Force).Count) { throw "Destination is not empty: $target. Choose a new directory; nothing was overwritten." }
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('LocalAssetStudio-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $headers = @{'User-Agent'='LocalAssetStudio-setup'}
    $revision = (Invoke-RestMethod 'https://api.github.com/repos/CakarK/localassetstudio/commits/main' -Headers $headers).sha
    if ($revision -notmatch '^[a-f0-9]{40}$') { throw 'Invalid repository revision.' }
    $archive = Join-Path $stage 'source.zip'
    Invoke-WebRequest "https://github.com/CakarK/localassetstudio/archive/$revision.zip" -OutFile $archive -UseBasicParsing
    Expand-Archive -LiteralPath $archive -DestinationPath $stage
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $stage "localassetstudio-$revision") -Force | Copy-Item -Destination $target -Recurse
    # Keep a receipt; a resumed bootstrap never replaces an existing installation.
    $revision | Set-Content -LiteralPath (Join-Path $target '.source-revision') -Encoding Ascii
    Remove-Item -LiteralPath $archive
}
$arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$entry)
if ($NoStart) { $arguments += '-NoStart' }
& powershell.exe @arguments
if ($LASTEXITCODE -ne 0) { throw "Installation did not complete. Rerun the same command to resume. Directory: $target" }
