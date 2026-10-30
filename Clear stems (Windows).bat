@echo off
rem Frees disk space by clearing split instruments from your songs. Close Instrumental Studio first.
cd /d "%~dp0" & (where python >nul 2>nul && (python clear_stems.py) || (py -3 clear_stems.py)) & echo. & pause & exit /b
