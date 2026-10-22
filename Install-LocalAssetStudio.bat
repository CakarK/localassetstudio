@echo off
setlocal
title Local Asset Studio - Setup
if exist "%~dp0Install-LocalAssetStudio.ps1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-LocalAssetStudio.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $script=Invoke-RestMethod 'https://raw.githubusercontent.com/CakarK/localassetstudio/v0.3.0/install.ps1'; & ([scriptblock]::Create($script)) -SourceRef 'v0.3.0'"
)
if errorlevel 1 (
  echo.
  echo Setup stopped. Read the message above, then double-click this file to resume.
  pause
  exit /b 1
)
endlocal
