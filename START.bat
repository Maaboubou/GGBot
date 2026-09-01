@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "STARTUP_ARG="
if /I "%~1"=="--startup" set "STARTUP_ARG=-Startup"

start "" /b powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%ROOT%scripts\launcher\bootstrap.ps1" %STARTUP_ARG%
exit /b 0
