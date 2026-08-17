@echo off
rem Orrery - double-click launcher for the local dev server launch pad.
rem
rem Prefers a windowless interpreter (pythonw / pyw) so nothing lingers on the
rem taskbar, and falls back to python.exe with a console when neither exists.
rem The browser is opened by orrery.py itself once the socket is listening; if
rem Orrery is already running, the second launch just opens the tab and exits.

setlocal
set "SCRIPT=%~dp0launcher\orrery.py"

if not exist "%SCRIPT%" (
    echo Could not find "%SCRIPT%".
    echo Run this from inside a checkout of the madirewolf repository.
    pause
    exit /b 1
)

where pythonw.exe >nul 2>&1
if %errorlevel%==0 goto windowless

where pyw.exe >nul 2>&1
if %errorlevel%==0 goto pylauncher

where python.exe >nul 2>&1
if %errorlevel%==0 goto console

echo Orrery needs Python 3.12 on PATH.
echo Install it from https://www.python.org/downloads/ and run this again.
pause
exit /b 1

:windowless
start "Orrery" /b pythonw.exe "%SCRIPT%" %*
exit /b 0

:pylauncher
start "Orrery" /b pyw.exe -3 "%SCRIPT%" %*
exit /b 0

:console
rem No windowless interpreter available: keep the console so errors stay visible.
python.exe "%SCRIPT%" %*
exit /b %errorlevel%
