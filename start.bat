@echo off
rem Start Job Watch (Windows cmd)
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
"%PY%" -m app.cli init
"%PY%" -m app.cli search-now
echo Serveur sur http://0.0.0.0:8000 (LAN: http://192.168.1.83:8000)
"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000