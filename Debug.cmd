@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start.ps1" -DebugMode
if errorlevel 1 pause
