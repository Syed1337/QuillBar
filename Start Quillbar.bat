@echo off
REM One-click launcher. Keep this file next to quillbar.py.
cd /d "%~dp0"

REM Find Python (py launcher first, then python on PATH)
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

REM Install libraries on first run only
%PY% -c "import PySide6, requests" >nul 2>nul
if errorlevel 1 (
  echo First run: installing PySide6 and requests...
  %PY% -m pip install --user PySide6 requests
  if errorlevel 1 (
    echo Install failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

if not exist "quillbar.py" (
  echo quillbar.py not found. Put this .bat in the same folder as quillbar.py.
  pause
  exit /b 1
)

REM Start without a console window
if "%PY%"=="py" (start "" pyw "quillbar.py") else (start "" pythonw "quillbar.py")
exit /b 0
