@echo off
REM Checks that pool jobs on this PC are really isolated (separate account, no access to your files,
REM no internet, memory and CPU limits).
cd /d "%~dp0"
venv\Scripts\python.exe win_sandbox.py --test
pause
