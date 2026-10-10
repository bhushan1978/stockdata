@echo off
rem Stop the Stock Data application server safely.
rem   stop_stockdata.bat                      asks what to do with running downloads
rem   stop_stockdata.bat --pause-downloads    pause them at a checkpoint, then stop the server
rem   stop_stockdata.bat --keep-downloads     stop only the server, downloads keep running
rem   stop_stockdata.bat --force [--kill-workers] [--yes]   RECOVERY ONLY: end a hung server immediately
rem Stopping the server never kills a download unless you ask for it with --force --kill-workers.
setlocal EnableExtensions
cd /d "%~dp0"
title StockData - stop

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [StockData] There is no virtual environment yet, so the application has never been started here. Nothing to stop.
  if not defined STOCKDATA_NO_WAIT pause
  exit /b 0
)
".venv\Scripts\python.exe" -m stockdata.web stop %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [StockData] The stop did not complete, exit code %RC%. Read the messages above.
  pause
  exit /b %RC%
)
if not defined STOCKDATA_NO_WAIT timeout /t 8 >nul
exit /b 0
