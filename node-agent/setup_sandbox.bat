@echo off
REM One-time setup of the Windows sandbox for pool jobs - no Docker needed.
REM Creates hidden low-privilege accounts that run the jobs. Asks for administrator permission.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_sandbox.ps1"
echo.
echo Restart the agent now (close its window, then run start_agent.bat).
pause
