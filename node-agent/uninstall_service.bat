@echo off
REM Stops and removes the CRMS node agent Windows service. Run start_agent.bat afterward if
REM you want to go back to running the agent manually in its own window.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1" -Remove
pause
