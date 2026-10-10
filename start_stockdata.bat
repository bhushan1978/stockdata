@echo off
rem Start the Stock Data application (or open it when it is already running).
rem   start_stockdata.bat            normal start, opens http://127.0.0.1:8765/
rem   start_stockdata.bat 9000       prefer another port (the next free one is used automatically when it is taken)
rem First start only: creates the virtual environment and installs the packages (needs Python 3.10+ and internet access).
rem Afterwards the application keeps running in the background. Stop it with stop_stockdata.bat.
setlocal EnableExtensions
cd /d "%~dp0"
title StockData - starting

set "LAUNCH="
if exist ".venv\Scripts\python.exe" set "LAUNCH=.venv\Scripts\python.exe"
if defined LAUNCH goto launch

rem No virtual environment yet: look for a Python 3.10 or newer to create it with.
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined LAUNCH (
    py -%%V -c "import sys" >nul 2>nul
    if not errorlevel 1 set "LAUNCH=py -%%V"
  )
)
if defined LAUNCH goto launch
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "LAUNCH=python"
if defined LAUNCH goto launch

echo.
echo [StockData] Python 3.10 or newer was not found.
echo             Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" in the installer.
echo             Then run start_stockdata.bat again.
pause
exit /b 1

:launch
%LAUNCH% -m stockdata.web.launcher %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto done
echo.
echo [StockData] The application could not be started. Read the messages above.
echo             More help: docs\WEBUI.md, section Troubleshooting. Server messages are in data\web\logs\server_console.log
pause
exit /b %RC%

:done
if not defined STOCKDATA_NO_WAIT timeout /t 6 >nul
exit /b 0
