@echo off
setlocal EnableExtensions

title GGBot GUI Launcher

set "ROOT=%~dp0"
if defined WXAUTOX_HOME (
    if exist "%WXAUTOX_HOME%\launcher.py" set "ROOT=%WXAUTOX_HOME%\"
)

cd /d "%ROOT%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Cannot enter project directory: %ROOT%
    pause
    exit /b 1
)

echo ============================================================
echo GGBot GUI Launcher
echo ============================================================
echo [INFO] Working directory: %CD%

if not exist "launcher.py" (
    echo [ERROR] launcher.py not found.
    echo [HINT] Do not copy this bat to Desktop. Create a shortcut to the original file.
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs" >nul 2>&1
if not exist "data" mkdir "data" >nul 2>&1

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo [HINT] Run Start.bat first.
    pause
    exit /b 1
)
set "PYTHON=%CD%\.venv\Scripts\python.exe"

for /f "delims=" %%V in ('"%PYTHON%" -V 2^>^&1') do set "PY_VER=%%V"
echo [INFO] Python: %PY_VER%
echo [INFO] Python path: %PYTHON%

set "LAUNCH_LOG=%CD%\logs\gui_launcher.log"
echo [INFO] Launch log: %LAUNCH_LOG%

"%PYTHON%" -c "import tkinter" >> "%LAUNCH_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] tkinter is missing in current Python.
    echo [HINT] Use a Python installation that includes tkinter.
    pause
    exit /b 1
)

echo [INFO] Starting launcher.py in foreground mode...
"%PYTHON%" "%CD%\launcher.py" >> "%LAUNCH_LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] launcher.py exited with code %EXIT_CODE%.
    echo [HINT] Check log: %LAUNCH_LOG%
    pause
    exit /b %EXIT_CODE%
)

echo [OK] launcher.py exited normally.
exit /b 0
