@echo off
rem Switches splitting to your NVIDIA graphics card (downloads the graphics-card version of PyTorch once, about 3 GB).
cd /d "%~dp0" & (where python >nul 2>nul && (python check_setup.py --gpu) || (py -3 check_setup.py --gpu)) & echo. & pause & exit /b
