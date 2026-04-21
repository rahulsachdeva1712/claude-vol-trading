@echo off
REM ============================================================
REM  volscalp one-click launcher (Windows)
REM  - creates .venv on first run
REM  - installs/updates the package in editable mode
REM  - launches the app against configs/default.yaml
REM  Double-click this file, or run `run.bat` from a terminal.
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

REM ---- Python check ----------------------------------------------------
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PY=py -3.11"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [volscalp] Python 3.11 not found on PATH. Install from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    set "PY=python"
)

REM ---- venv bootstrap --------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [volscalp] Creating virtual environment at .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [volscalp] Failed to create venv.
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv\Scripts\python.exe"

REM ---- dependency install (idempotent) ---------------------------------
echo [volscalp] Ensuring dependencies are installed ...
"%VENV_PY%" -m pip install --upgrade pip >nul
"%VENV_PY%" -m pip install -e .
if errorlevel 1 (
    echo [volscalp] pip install failed. See messages above.
    pause
    exit /b 1
)

REM ---- .env check ------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        echo [volscalp] No .env found. Copying .env.example -> .env
        copy /Y ".env.example" ".env" >nul
        echo [volscalp] Edit .env and paste your Dhan DHAN_ACCESS_TOKEN before going LIVE.
    )
)

REM ---- launch ----------------------------------------------------------
echo [volscalp] Starting app ... (Ctrl+C to stop)
echo [volscalp] Dashboard will be at http://127.0.0.1:8765
echo.
"%VENV_PY%" -m volscalp --config configs\default.yaml

echo.
echo [volscalp] Process exited.
pause
endlocal
