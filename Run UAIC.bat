@echo off
rem Double-click to start the Universal AI Coprocessor desktop app.
rem
rem Runs from the project's .venv if it exists, otherwise from the packaged
rem build in dist\UAIC. The Gemini key is read from your Windows user
rem environment (set once with: setx UAIC_GEMINI_API_KEY "your-key").
rem It is never written into this file and never printed.
rem Any arguments are passed through, e.g.:  "Run UAIC.bat" --data-dir D:\uaic

setlocal
cd /d "%~dp0"

rem A window opened before `setx` ran does not see the new value, so fall back
rem to reading it from the registry. Nothing is echoed.
if not defined UAIC_GEMINI_API_KEY (
    for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v UAIC_GEMINI_API_KEY 2^>nul ^| find /i "UAIC_GEMINI_API_KEY"') do set "UAIC_GEMINI_API_KEY=%%B"
)
if not defined UAIC_GEMINI_MODEL (
    for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v UAIC_GEMINI_MODEL 2^>nul ^| find /i "UAIC_GEMINI_MODEL"') do set "UAIC_GEMINI_MODEL=%%B"
)

if exist ".venv\Scripts\pythonw.exe" goto :from_source
if exist "dist\UAIC\UAIC.exe" goto :from_build

echo Could not find the app.
echo.
echo Either create the virtual environment:
echo     py -m venv .venv
echo     .venv\Scripts\python -m pip install -r requirements-desktop.txt
echo or build it:  py scripts\build_desktop.py --with-model
echo.
pause
exit /b 1

:from_source
rem Check the app can import before launching windowless, so a broken
rem install shows an error here instead of silently doing nothing.
".venv\Scripts\python.exe" -c "import PySide6, uaic.desktop.window" 2> "%TEMP%\uaic_start_error.txt"
if errorlevel 1 (
    echo The app could not start:
    echo.
    type "%TEMP%\uaic_start_error.txt"
    echo.
    echo Try:  .venv\Scripts\python -m pip install -r requirements-desktop.txt
    echo.
    pause
    exit /b 1
)
if not defined UAIC_GEMINI_API_KEY (
    echo Note: UAIC_GEMINI_API_KEY is not set. The app will open, but planning
    echo is disabled until you run:  setx UAIC_GEMINI_API_KEY "your-key"
    timeout /t 4 >nul
)
start "" ".venv\Scripts\pythonw.exe" -m uaic.desktop %*
exit /b 0

:from_build
start "" "dist\UAIC\UAIC.exe" %*
exit /b 0
