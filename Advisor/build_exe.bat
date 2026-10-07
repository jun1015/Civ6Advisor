@echo off
rem ===================================================================
rem  Pack the overlay into a single-file exe (the Chinese exe name is
rem  produced by build_exe.py, not here).
rem
rem  Keep this file PURE ASCII on purpose: cmd parses .bat with the
rem  console code page, so a Chinese argument written here easily turns
rem  into mojibake - you would get an exe whose name is garbage.
rem  All Chinese text lives in build_exe.py (same pattern as
rem  sync_mod.bat -> sync_mod.py).
rem ===================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found on PATH.
  echo     Install Python 3 first, then run this file again.
  pause
  exit /b 1
)

chcp 65001 >nul
python "%~dp0build_exe.py" %*
if errorlevel 1 (
  echo.
  echo [!] Build failed - the reason is printed above.
  pause
)
endlocal
