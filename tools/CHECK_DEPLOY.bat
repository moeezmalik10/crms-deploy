@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0CHECK_DEPLOY.ps1" %*
echo.
pause
