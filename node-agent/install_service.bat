@echo off
REM Installs the agent as a Windows service with a watchdog (NSSM) - no more manual
REM start_agent.bat window, and it restarts itself automatically if it crashes or the PC
REM reboots. Asks for administrator permission. Needs nssm.exe - see install_service.ps1
REM for where to put it if this stops with "nssm.exe not found".
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1"
pause
