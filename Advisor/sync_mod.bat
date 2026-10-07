@echo off
rem ===================================================================
rem  Sync the in-game mod into Civ6's Mods folder.
rem
rem  The real logic lives in sync_mod.py, not here, because writing
rem  Chinese text inside a .bat is a guaranteed encoding headache.
rem  This file is deliberately 100% ASCII.
rem ===================================================================

cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if exist "%PY%" (
    "%PY%" sync_mod.py
    goto :end
)

where python >nul 2>nul
if not errorlevel 1 (
    python sync_mod.py
    goto :end
)

echo.
echo Python not found. Copy the folder by hand instead:
echo   from: Civ6Mod\AdvisorMod\Civ6AdvisorBridge
echo   to:   %%USERPROFILE%%\Documents\My Games\Sid Meier's Civilization VI\Mods\
echo.

:end
pause
