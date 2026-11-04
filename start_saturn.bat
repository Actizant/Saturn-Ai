@echo off
setlocal EnableExtensions EnableDelayedExpansion

title Saturn - Launcher
cd /d "%~dp0"

color 0E

echo ==========================================================================
echo                            S A T U R N
echo          AUTONOMOUS SELF-EVOLUTION COGNITIVE ENGINE
echo    [Skill Forge // Crucible Sandbox // 180 FPS HoloCore]
echo ==========================================================================
echo.

rem Launch under the validated .venv312 (Python 3.12) runtime. This is the
rem stack with the pinned native wheels (PyQt6, torch cu124, google-genai,
rem opencv, onnx) that the app is built against. Running on the system
rem Python 3.14 instead causes a native heap-corruption crash at startup.
set "VENV_PYW=%~dp0.venv312\Scripts\pythonw.exe"
set "VENV_PY=%~dp0.venv312\Scripts\python.exe"

if exist "%VENV_PYW%" (
  echo Starting Saturn under .venv312 (validated runtime)...
  start "" "%VENV_PYW" main.py --startup
  exit /b 0
)

if exist "%VENV_PY%" (
  echo Starting Saturn under .venv312 (console)...
  start "" "%VENV_PY" main.py --startup
  exit /b 0
)

echo .venv312 not found. Running bootstrap to build the environment...
powershell.exe -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"

if %errorlevel% neq 0 (
  echo ERROR: Bootstrap failed.
  pause
  exit /b %errorlevel%
)
exit /b 0
