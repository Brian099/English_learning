@echo off
REM Local dev server (Windows). Double-click or run from a terminal.
REM Bypasses the PowerShell execution policy and calls dev_server.ps1.

cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev_server.ps1"
pause
