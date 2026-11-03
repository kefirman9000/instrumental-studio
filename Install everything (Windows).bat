@echo off
rem Instrumental Studio: installs everything it needs on a computer that has nothing yet.
rem   1. Python 3.12 (if no Python 3.10-3.13 is found), for you only: no admin rights needed
rem   2. ffmpeg (reads MP3, M4A, FLAC...)
rem   3. The app's Python packages
rem   4. Offers the separation model download at the end
rem Safe to run again: it skips anything that's already there.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Instrumental Studio setup
echo.
echo  Instrumental Studio setup
echo  =========================
echo  This installs everything the app needs. It takes about 10 minutes, mostly downloads.
echo.

set "PYVER=3.12.8"
set "PY="
call :find_python
if defined PY goto have_python

echo [1/4] Installing Python %PYVER% ...
where winget >nul 2>nul
if not errorlevel 1 (
    winget install --id Python.Python.3.12 -e --scope user --silent --accept-source-agreements --accept-package-agreements
)
call :find_python
if defined PY goto have_python

echo        winget isn't available or didn't work; downloading the installer from python.org instead.
set "PYEXE=%TEMP%\python-%PYVER%-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe' -OutFile '%PYEXE%'"
if not exist "%PYEXE%" (
    echo.
    echo  Couldn't download Python. Check your internet connection, or install Python 3.12 yourself from
    echo  https://www.python.org/downloads/ ^(tick "Add Python to PATH"^), then run this again.
    goto done
)
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%PYEXE%" >nul 2>nul
call :find_python
if not defined PY (
    echo.
    echo  Python was installed, but this window can't see it yet. Close this window and run this file again.
    goto done
)

:have_python
echo [1/4] Python: !PY!
echo.

echo [2/4] ffmpeg ...
where ffmpeg >nul 2>nul
if not errorlevel 1 (
    echo        already installed
) else (
    where winget >nul 2>nul
    if not errorlevel 1 (
        winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements
    ) else (
        echo        winget isn't available: the setup check below will tell you how to get ffmpeg.
    )
)
echo.

echo [3/4] The app's packages ...
"!PY!" -m ensurepip --upgrade >nul 2>nul
"!PY!" -m pip install --upgrade pip >nul 2>nul
"!PY!" -m pip install -r requirements.txt
echo.

echo [4/4] Checking everything ^(and offering the separation model download^) ...
echo.
"!PY!" check_setup.py

:done
echo.
echo  When everything shows OK, start the app with "Instrumental Studio.pyw"
echo  ^(or "Start Instrumental Studio (Windows).bat"^).
echo.
pause
exit /b

rem ---------------------------------------------------------------- find a Python 3.10-3.13
:find_python
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul && for /f "delims=" %%P in ('py -%%V -c "import sys;print(sys.executable)"') do set "PY=%%P"
    )
)
if not defined PY (
    for %%D in ("%LocalAppData%\Programs\Python\Python312" "%LocalAppData%\Programs\Python\Python313" "%LocalAppData%\Programs\Python\Python311" "%ProgramFiles%\Python312" "%ProgramFiles%\Python313") do (
        if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
    )
)
if not defined PY (
    python -c "import sys;exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)"') do set "PY=%%P"
)
exit /b
