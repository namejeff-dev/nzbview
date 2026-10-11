@echo off
setlocal
cd /d "%~dp0"
set "NZBVIEW_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "NZBVIEW_PYTHON=%~dp0.venv\Scripts\python.exe"
"%NZBVIEW_PYTHON%" "%~dp0rebuild_search_data.py"
pause
endlocal
