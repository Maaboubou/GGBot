@echo off
setlocal EnableExtensions
chcp 65001 >nul

title Mabobot First Run and Launcher
set "ROOT=%~dp0"
cd /d "%ROOT%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Cannot enter the project directory: %ROOT%
    pause
    exit /b 1
)

echo ============================================================
echo Mabobot Windows Setup and Launcher
echo ============================================================

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\install.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Setup or environment validation failed.
    echo [HINT] Review the error above, or run Install.bat and retry.
    pause
    exit /b 1
)

call "%ROOT%Start-GUI.bat"
exit /b %ERRORLEVEL%
