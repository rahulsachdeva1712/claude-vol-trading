@echo off
REM ============================================================
REM  volscalp one-click launcher (Windows)
REM  - picks the newest Python >= 3.11 available via py launcher
REM  - creates .venv on first run
REM  - installs/updates the package in editable mode
REM  - launches the app against configs/default.yaml
REM  Double-click this file, or run `run.bat` from a terminal.
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

REM ---- Python selection (>= 3.11, newest first) -----------------------
set "PY="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    for %%V in (3.14 3.13 3.12 3.11) do (
        if not defined PY (
            py -%%V -c "import sys" >nul 2>&1
            if !ERRORLEVEL!==0 set "PY=py -%%V"
        )
    )
)

if not defined PY (
    where python >nul 2>&1
    if %ERRORLEVEL%==0 (
        for /f "delims=" %%V in ('python -c "import sys; print(1 if sys.version_info[:2] >= (3,11) else 0)" 2^>nul') do set "PYOK=%%V"
        if "!PYOK!"=="1" set "PY=python"
    )
)

if not defined PY (
    echo [volscalp] No Python ^>=3.11 found.
    echo [volscalp] Install Python 3.11 from https://www.python.org/downloads/release/python-3119/
    echo [volscalp] ^(check "Add python.exe to PATH" during install^)
    pause
    exit /b 1
)

echo [volscalp] Using interpreter: %PY%

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

REM Fire-and-forget: wait ~5 seconds for uvicorn to bind the port, then
REM open the dashboard in the default browser. Runs in a detached cmd so
REM the foreground app launch below is unaffected. Skip by setting
REM VOLSCALP_NO_BROWSER=1 before calling run.bat.
if not defined VOLSCALP_NO_BROWSER (
    start "" /B cmd /c "timeout /t 5 /nobreak >nul 2>&1 && start "" http://127.0.0.1:8765"
)

"%VENV_PY%" -m volscalp --config configs\default.yaml

echo.
echo [volscalp] Process exited.
pause
endlocal
