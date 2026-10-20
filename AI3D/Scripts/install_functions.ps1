Set-StrictMode -Version 2
# PowerShell 7 callers can pass a module path that omits Windows PowerShell's
# inbox modules. Adjust this process only; do not edit the user's profile/PATH.
if ($env:OS -eq 'Windows_NT') {
    $windowsModules=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/Modules'
    if (($env:PSModulePath -split ';') -notcontains $windowsModules) { $env:PSModulePath += ';' + $windowsModules }
}

function Invoke-LASCommand {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')" }
}

function Get-LASFileSHA256 {
    param([string]$Path)
    $stream=[IO.File]::OpenRead($Path)
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose();$stream.Dispose() }
}

function Get-LASVerifiedArchive {
    param($Spec, [string]$Cache)
    New-Item -ItemType Directory -Path $Cache -Force | Out-Null
    $path = Join-Path $Cache ($Spec.sha256 + '.zip')
    if (Test-Path -LiteralPath $path) {
        if ((Get-LASFileSHA256 $path) -ne $Spec.sha256) { throw "Existing cache hash mismatch; file preserved: $path" }
        return $path
    }
    $partial = $path + '.partial'
    Write-Host "Download: $($Spec.url)"
    # Windows 11 includes curl.exe. Partial files are only promoted after SHA256.
    & curl.exe --fail --location --retry 3 --retry-delay 2 --continue-at - --output $partial $Spec.url
    if ($LASTEXITCODE -ne 0) { throw "Download interrupted; rerun to resume: $partial" }
    if ((Get-Item -LiteralPath $partial).Length -ne $Spec.size -or (Get-LASFileSHA256 $partial) -ne $Spec.sha256) {
        $quarantine = $partial + '.bad-' + [guid]::NewGuid().ToString('N')
        Move-Item -LiteralPath $partial -Destination $quarantine
        throw "Downloaded archive failed verification; quarantined: $quarantine"
    }
    Move-Item -LiteralPath $partial -Destination $path
    return $path
}

function Install-LASTool {
    param([string]$Name, $Spec, [string]$Tools, [string]$Cache)
    $directory = Join-Path $Tools ($Name + '-' + $Spec.version)
    $receipt = Join-Path $directory '.verified-archive-sha256'
    $complete = Join-Path $directory '.extraction-complete'
    $binary = Join-Path $directory $Spec.executable
    if (Test-Path -LiteralPath $directory) {
        if (!(Test-Path -LiteralPath $receipt) -or (Get-Content -LiteralPath $receipt -Raw).Trim() -ne $Spec.sha256) { throw "Unowned or incomplete tool directory preserved: $directory" }
        if ((Test-Path -LiteralPath $binary) -and (Test-Path -LiteralPath $complete)) { return $binary }
    }
    $archive = Get-LASVerifiedArchive $Spec $Cache
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    # Receipt also allows an interrupted extraction to resume on the next run.
    $Spec.sha256 | Set-Content -LiteralPath $receipt -Encoding Ascii
    Expand-Archive -LiteralPath $archive -DestinationPath $directory -Force
    if (!(Test-Path -LiteralPath $binary)) { throw "Archive did not contain expected executable: $binary" }
    $Spec.sha256 | Set-Content -LiteralPath $complete -Encoding Ascii
    return $binary
}

function Install-LASSource {
    param([string]$Git, $Spec, [string]$Destination)
    if (!(Test-Path -LiteralPath $Destination)) {
        $absolute = [IO.Path]::GetFullPath($Destination)
        $stage = $absolute + '.installing'
        $owner = $stage + '.owner'
        $identity = $Spec.url + '@' + $Spec.revision
        if (Test-Path -LiteralPath $stage) {
            if (!(Test-Path -LiteralPath $owner) -or (Get-Content -LiteralPath $owner -Raw).Trim() -ne $identity) { throw "Unowned staging directory preserved: $stage" }
        } else { $identity | Set-Content -LiteralPath $owner -Encoding Ascii }
        if (!(Test-Path -LiteralPath (Join-Path $stage '.git'))) { Invoke-LASCommand $Git @('clone','--no-checkout','--filter=blob:none',$Spec.url,$stage) }
        Invoke-LASCommand $Git @('-C',$stage,'fetch','--depth','1','origin',$Spec.revision)
        Invoke-LASCommand $Git @('-C',$stage,'checkout','--detach',$Spec.revision)
        if ([IO.Path]::GetDirectoryName($stage) -ne [IO.Path]::GetDirectoryName($absolute) -or $stage -ne ($absolute + '.installing')) { throw 'Source staging path validation failed.' }
        Move-Item -LiteralPath $stage -Destination $absolute
        Remove-Item -LiteralPath $owner
    }
    $revision = (& $Git -C $Destination rev-parse HEAD)
    if ($LASTEXITCODE -ne 0 -or "$revision".Trim() -ne $Spec.revision) { throw "Unexpected source revision at $Destination; no reset or deletion performed." }
}

function Test-LASHardware {
    param($Lock, [string]$Root)
    if ($env:OS -ne 'Windows_NT' -or ![Environment]::Is64BitProcess) { throw 'The full installer supports 64-bit Windows 11 only.' }
    $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (!$smi) { throw 'NVIDIA driver / nvidia-smi missing. Install the NVIDIA driver, restart Windows and rerun.' }
    $rows = @(& $smi.Source --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits)
    if ($LASTEXITCODE -ne 0 -or !$rows.Count) { throw 'NVIDIA GPU inspection failed.' }
    # The current pipeline uses GPU 0; a larger secondary GPU is not sufficient.
    $parts = $rows[0] -split ','
    if ([int]$parts[1].Trim() -lt $Lock.minimum_vram_mib) { throw 'GPU 0 needs at least 24 GB dedicated VRAM for the full pipeline.' }
    $disk = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($Root))
    $required = [int64]$Lock.minimum_free_disk_gib * 1GB
    foreach ($spec in $Lock.files) {
        $present = Join-Path $Root $spec.path
        if ((Test-Path -LiteralPath $present) -and (Get-Item -LiteralPath $present).Length -eq $spec.size) { $required -= [int64]$spec.size }
    }
    foreach ($spec in $Lock.huggingface_cache) {
        $present = Join-Path $Root ('AI3D/Models/huggingface/hub/models--' + $spec.repository.Replace('/','--') + '/snapshots/' + $spec.revision + '/' + $spec.remote_file)
        if ((Test-Path -LiteralPath $present) -and (Get-Item -LiteralPath $present).Length -eq $spec.size) { $required -= [int64]$spec.size }
    }
    if (Test-Path -LiteralPath (Join-Path $Root 'AI3D/.venv/Scripts/python.exe')) { $required -= 30GB }
    if (Test-Path -LiteralPath (Join-Path $Root 'AI3D/ImageComfyUI/.venv/Scripts/python.exe')) { $required -= 30GB }
    $required = [math]::Max(20GB, $required)
    if ($disk.AvailableFreeSpace -lt $required) { throw "Need $([math]::Ceiling($required / 1GB)) GiB free on the installation drive (fresh full install: $($Lock.minimum_free_disk_gib) GiB)." }
    Write-Host "GPU: $($rows[0]); free disk: $([math]::Round($disk.AvailableFreeSpace / 1GB)) GiB"
}
