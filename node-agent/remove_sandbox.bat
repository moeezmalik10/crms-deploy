@echo off
REM Removes the Windows sandbox (accounts, permissions, firewall rule, quotas). Stop the agent first.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_sandbox.ps1" -Remove
pause
