@echo off
rem DEVELOPERS ONLY. Normal users never need this: the interface is prebuilt in webui\dist.
rem Rebuilds webui\dist after changing files under webui\src. Needs Node.js 20 or newer.
setlocal EnableExtensions
cd /d "%~dp0..\..\webui"
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Install Node.js 20 or newer from https://nodejs.org/
  exit /b 1
)
if exist package-lock.json ( call npm ci ) else ( call npm install )
if errorlevel 1 exit /b 1
call npm run build
exit /b %ERRORLEVEL%
