@echo off
title CRMS Agent
rem Remote sessions change the RustDesk password, which needs administrator rights.
net session >nul 2>&1
if errorlevel 1 (
  echo Asking for administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d %~dp0
echo Backend: & type backend_url.txt
venv\Scripts\python.exe agent.py
pause
