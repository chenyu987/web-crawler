@echo off
REM Windows: double-click this file to run webcrawler.
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3 not found. Install it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

echo First run: installing dependencies, please wait...
%PY% -m venv .venv || (pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)

:run
".venv\Scripts\python.exe" -m webcrawler %*
echo.
pause
