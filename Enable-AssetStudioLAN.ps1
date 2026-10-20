# Run once from an Administrator PowerShell to allow only computers on the local subnet.
param([int]$Port = 8088)
$ErrorActionPreference = 'Stop'
$studioRule = 'Game3DCreator Asset Studio LAN'
if ($Port -ne 8088) { $studioRule += " $Port" }
$pythonLauncher = Join-Path $PSScriptRoot 'AI3D\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonLauncher)) { throw 'Proje Python ortamı bulunamadı.' }
# Windows virtualenv can redirect to a base Python process; match the real socket owner.
$pythonPath = (& $pythonLauncher -c 'import psutil; print(psutil.Process().exe())').Trim()
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Çalışan Python binary yolu belirlenemedi.' }
$pythonPath = (Resolve-Path -LiteralPath $pythonPath).ProviderPath
if (Get-NetFirewallRule -DisplayName $studioRule -ErrorAction SilentlyContinue) {
    Write-Host 'Mevcut kural korundu.'
} else {
    New-NetFirewallRule -DisplayName $studioRule -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private -Program $pythonPath | Out-Null
    Write-Host "Özel yerel ağda TCP $Port erişimi açıldı."
}
