@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_windows.ps1" %*

set "START_RESULT=%errorlevel%"
if not "%START_RESULT%"=="0" if not "%START_RESULT%"=="130" pause
exit /b %START_RESULT%
