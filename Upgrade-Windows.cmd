@echo off
setlocal
set "project=%~1"
if not defined project (
  set "interactive=1"
  set /p "project=Installed original project directory: "
)
if not defined project exit /b 1
if not exist "%project%\.venv\Scripts\python.exe" (
  echo Install the original project and its Python environment first.
  exit /b 1
)
"%project%\.venv\Scripts\python.exe" -B "%~dp0upgrade.py" apply --project "%project%"
set "upgrade_exit=%errorlevel%"
if not "%upgrade_exit%"=="0" exit /b %upgrade_exit%
echo [ais-pg-upgrade] Patch files processed. Restart the original service to load changes:
echo   "%project%\start.bat"
echo Keep the service window open and run only one instance.
if defined interactive pause
exit /b 0
