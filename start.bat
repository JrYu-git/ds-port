@echo off
setlocal
cd /d "%~dp0"

set "PYCMD="

where py >nul 2>nul
if %errorlevel%==0 set "PYCMD=py -3"

if not defined PYCMD (
  where python >nul 2>nul
  if errorlevel 1 goto nopython
  set "PYCMD=python"
)

echo.
echo   Starting ds-port ...
echo   Your browser will open automatically.
echo   Close this window to stop.
echo.

%PYCMD% server.py

if errorlevel 1 (
  echo.
  echo   [!] server exited with an error.
  pause
)
exit /b 0

:nopython
echo.
echo   [x] Python not found
echo.
echo   Install Python 3.9 or newer from:
echo   https://www.python.org/downloads/
echo.
echo   During install, tick "Add Python to PATH".
echo.
pause
exit /b 1
