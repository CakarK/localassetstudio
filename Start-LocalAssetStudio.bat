@echo off
setlocal
title Local Asset Studio
if not exist "%~dp0AI3D\State\install-verification.json" (
  call "%~dp0Install-LocalAssetStudio.bat"
  if errorlevel 1 exit /b 1
  exit /b 0
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-LocalAssetStudio.ps1"
if errorlevel 1 pause
endlocal
