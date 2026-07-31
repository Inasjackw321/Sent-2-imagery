@echo off
rem Windows: double-click to launch Sent-2.
rem Creates a local virtual environment on first run, then opens the app window.

cd /d "%~dp0.."

where python >nul 2>&1
if errorlevel 1 (
  echo Python is not installed. Get it from https://www.python.org/downloads/
  echo Remember to tick "Add python.exe to PATH" in the installer.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Setting up ^(first run only^)...
  python -m venv .venv || goto :failed
)

call .venv\Scripts\activate.bat

if not exist ".venv\.deps-installed" (
  echo Installing dependencies...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || goto :failed
  echo. > .venv\.deps-installed
)

start "" pythonw app.py %*
exit /b 0

:failed
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
