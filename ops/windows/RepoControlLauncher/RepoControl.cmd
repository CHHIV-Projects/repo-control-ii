@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-repo-control.ps1"
if errorlevel 1 (
  echo Repo Control launcher failed. Review the message above.
  pause
  exit /b 1
)
exit /b 0
