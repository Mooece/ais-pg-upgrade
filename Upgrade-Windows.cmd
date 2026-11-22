@echo off
setlocal
set "project=%~1"
if not defined project set /p "project=Installed original project directory: "
if not defined project exit /b 1
if not exist "%project%\.venv\Scripts\python.exe" (
  echo Install the original project and its Python environment first.
  exit /b 1
)
"%project%\.venv\Scripts\python.exe" -B "%~dp0upgrade.py" apply --project "%project%"
exit /b %errorlevel%
