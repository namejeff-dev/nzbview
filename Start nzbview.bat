@echo off
setlocal
cd /d "%~dp0"
title nzbview v2.15.2
set "NZBVIEW_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "NZBVIEW_PYTHON=%~dp0.venv\Scripts\python.exe"
echo Keep this window open while nzbview is running.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8787'"
"%NZBVIEW_PYTHON%" "%~dp0run.py" --quiet
if errorlevel 1 pause
endlocal
