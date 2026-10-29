@echo off
rem Starts Instrumental Studio without leaving a console window open.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
) else (
  python app.py
  if errorlevel 1 pause
)
