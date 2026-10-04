@echo off
title CRMS agent setup
cd /d %~dp0
if exist venv rmdir /s /q venv
py -3.12 -m venv venv || python -m venv venv
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt || goto :fail
echo.
echo Agent setup finished. Check backend_url.txt, then run start_agent.bat
pause
exit /b 0
:fail
echo SETUP FAILED - is Python 3.12 installed with 'Add to PATH'?
pause
exit /b 1