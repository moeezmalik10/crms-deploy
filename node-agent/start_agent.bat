@echo off
title CRMS Agent
cd /d %~dp0
echo Backend: & type backend_url.txt
venv\Scripts\python.exe agent.py
pause