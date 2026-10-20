param([int]$Seconds=300)
$taskRoot=Split-Path $PSScriptRoot -Parent
$file=Join-Path $taskRoot ('Logs\gpu-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.csv')
'timestamp,name,memory_used_MiB,memory_total_MiB,gpu_util_percent' | Set-Content $file
for ($i=0;$i -lt $Seconds;$i+=2) {
    nvidia-smi --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits | Add-Content $file
    Start-Sleep -Seconds 2
}
Write-Host $file
