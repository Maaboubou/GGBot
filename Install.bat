@echo off
setlocal EnableExtensions
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Force
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo [OK] Setup completed. Run Start.bat to launch GGBot.
) else (
    echo [ERROR] Setup failed. Review the error above.
)
pause
exit /b %EXIT_CODE%
