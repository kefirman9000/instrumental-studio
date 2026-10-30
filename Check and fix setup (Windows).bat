@echo off
rem Checks everything Instrumental Studio needs and offers to install what is missing.
cd /d "%~dp0" & (where python >nul 2>nul && (python check_setup.py) || (py -3 check_setup.py)) & echo. & pause & exit /b
