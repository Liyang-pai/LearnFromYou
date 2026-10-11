@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python runtime missing: .venv\Scripts\python.exe
    pause
    exit /b 1
)

".venv\Scripts\python.exe" run.py --open-browser

set "START_RESULT=%errorlevel%"
if not "%START_RESULT%"=="0" pause
exit /b %START_RESULT%
