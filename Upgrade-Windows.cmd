@echo off
setlocal
set "interactive="
set "project=%~1"
set "port=%~2"
if not defined port set "port=8080"
if not defined project (
  set "interactive=1"
  set /p "project=Installed original project directory: "
)
if not defined project exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Upgrade-Windows.ps1" -ProjectPath "%project%" -Port "%port%" -NoStart
set "upgrade_exit=%errorlevel%"
if defined interactive pause
exit /b %upgrade_exit%
