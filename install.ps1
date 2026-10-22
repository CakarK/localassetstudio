# Windows PowerShell 5.1 bootstrap. No Git/Python or administrator required.
param([string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'LocalAssetStudio'), [switch]$NoStart, [string]$SourceRef='main')
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or ![Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell on Windows 11.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$windowsModules=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/Modules'
if (($env:PSModulePath -split ';') -notcontains $windowsModules) { $env:PSModulePath += ';' + $windowsModules }
$target = [IO.Path]::GetFullPath($InstallDirectory)
$entry = Join-Path $target 'Install-LocalAssetStudio.ps1'
$expectedVersion='0.3.0'
if (Test-Path -LiteralPath $entry) {
    $versionFile=Join-Path $target 'VERSION'
    $existingVersion=if (Test-Path -LiteralPath $versionFile) { (Get-Content -LiteralPath $versionFile -Raw).Trim() } else { '' }
    if ($existingVersion -ne $expectedVersion) {
        # Never dispatch a previous Hunyuan installer or overwrite its user data.
        $target=$target+'-v'+$expectedVersion
        $entry=Join-Path $target 'Install-LocalAssetStudio.ps1'
        Write-Host "Previous installation preserved. Installing TRELLIS release in: $target"
        if (Test-Path -LiteralPath $entry) {
            $newVersion=Join-Path $target 'VERSION'
            if (!(Test-Path -LiteralPath $newVersion) -or (Get-Content -LiteralPath $newVersion -Raw).Trim() -ne $expectedVersion) { throw 'Versioned destination is owned by another installation.' }
        }
    }
}
if (!(Test-Path -LiteralPath $entry)) {
    if ((Test-Path -LiteralPath $target) -and @(Get-ChildItem -LiteralPath $target -Force).Count) { throw "Destination is not empty: $target. Choose a new directory; nothing was overwritten." }
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('LocalAssetStudio-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $headers = @{'User-Agent'='LocalAssetStudio-setup'}
    if ($SourceRef -notmatch '^[a-zA-Z0-9._/-]+$') { throw 'Invalid source ref.' }
    $revision = (Invoke-RestMethod ("https://api.github.com/repos/CakarK/localassetstudio/commits/"+[Uri]::EscapeDataString($SourceRef)) -Headers $headers).sha
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
