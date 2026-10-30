@echo off
rem Updates Instrumental Studio without opening it. Everything runs from the one line below, because
rem the update replaces this file while it runs.
cd /d "%~dp0" & (where python >nul 2>nul && (python update.py) || (py -3 update.py)) & echo. & pause & exit /b
