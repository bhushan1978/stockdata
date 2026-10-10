@echo off
rem DEVELOPERS ONLY. Runs the backend in its own window plus the Vite dev server with hot reload (http://localhost:5173).
rem Needs the virtual environment (run start_stockdata.bat once) and Node.js 20 or newer.
setlocal EnableExtensions
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
if not exist ".venv\Scripts\python.exe" (
  echo Run start_stockdata.bat once first, so the virtual environment exists.
  pause
  exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Install Node.js 20 or newer from https://nodejs.org/
  pause
  exit /b 1
)
start "StockData backend (dev)" /D "%ROOT%" cmd /k .venv\Scripts\python.exe -m stockdata.web serve --dev
cd webui
if not exist node_modules (
  call npm install
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
call npm run dev -- --open
exit /b %ERRORLEVEL%
