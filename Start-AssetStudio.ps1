param([int]$Port = 8088, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$studioRoot = $PSScriptRoot
$pythonPath = Join-Path $studioRoot 'AI3D\.venv\Scripts\python.exe'
$logRoot = Join-Path $studioRoot 'AI3D\Logs\Web'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $studioRoot 'AI3D\State') | Out-Null
$runningStudio = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if (-not $runningStudio) {
    $studioProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m','ai3d_manager.web_app','--port',"$Port") -WorkingDirectory $studioRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'server.log') -RedirectStandardError (Join-Path $logRoot 'server-error.log')
    $studioProcess.Id | Set-Content (Join-Path $studioRoot 'AI3D\State\web-server.pid')
    for ($studioAttempt=0; $studioAttempt -lt 30; $studioAttempt++) {
        Start-Sleep -Milliseconds 400
        try { $studioResponse=Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 1; if ($studioResponse.StatusCode -eq 200) { break } } catch {}
        if ($studioProcess.HasExited) { throw "Stüdyo başlatılamadı. AI3D/Logs/Web/server-error.log dosyasına bakın." }
    }
}
$studioCheck = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5
if ($studioCheck.Headers['X-Asset-Studio'] -ne 'game3dcreator') { throw "Port $Port başka bir uygulamaya ait. Başka port seçin." }
Write-Host "`nLocal Asset Studio: http://localhost:$Port" -ForegroundColor Green
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } | ForEach-Object { Write-Host "Yerel ağ: http://$($_.IPAddress):$Port" }
$codeFile = Join-Path $studioRoot 'AI3D\State\web-access-code.txt'
if (Test-Path -LiteralPath $codeFile) { Write-Host "Erişim kodu: $([System.IO.File]::ReadAllText($codeFile))" }
Write-Host 'Görsel üretimi için mevcut Start-AI3D ve Start-ImageProviders servisleri açık olmalı.'
if (-not $NoBrowser) { Start-Process "http://localhost:$Port" }
