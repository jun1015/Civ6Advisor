@echo off
rem ===================================================================
rem  Command-line version (shows every step it takes).
rem ===================================================================
rem  Keep this file PURE ASCII: cmd parses .bat with the console code
rem  page, so Chinese text written here turns into mojibake.
rem
rem  Where Python comes from:
rem    - the Microsoft Store build puts it in WindowsApps and exposes
rem      it on PATH as an "app execution alias"
rem    - a python.org install puts python.exe on PATH directly
rem  Try the alias first, then PATH. On the machine this was written
rem  on, `where python` finds nothing at all - so the alias is not a
rem  fallback, it is the normal path there.
rem ===================================================================

setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if exist "%PY%" goto :run

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found on PATH.
  echo     Install Python 3 first - the Microsoft Store build is fine.
  pause
  exit /b 1
)

:run
"%PY%" agent.py %*

echo.
pause
endlocal
