@echo off
setlocal EnableExtensions

title WeChat Auto Login Bootstrap

set "ROOT=%~dp0"
cd /d "%ROOT%" >nul 2>&1
if errorlevel 1 exit /b 1

if not exist "logs" mkdir "logs" >nul 2>&1

set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%CD%\.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON=%CD%\venv\Scripts\python.exe"
)

if not defined PYTHON (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        set "PYTHON=%%I"
        goto :PythonFound
    )
)

:PythonFound
if not defined PYTHON exit /b 1

set "BOOT_LOG=%CD%\logs\wechat_auto_login_bat.log"
"%PYTHON%" "%CD%\wechat_auto_login.py" >> "%BOOT_LOG%" 2>&1
exit /b %ERRORLEVEL%
