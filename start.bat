@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PYCMD=
where py >nul 2>nul && set PYCMD=py
if not defined PYCMD (
  where python >nul 2>nul && set PYCMD=python
)

if not defined PYCMD (
  echo.
  echo   [x] 没有找到 Python
  echo.
  echo   请先安装 Python 3.9 或更高版本：
  echo   https://www.python.org/downloads/
  echo.
  echo   安装时记得勾选 "Add Python to PATH"
  echo.
  pause
  exit /b 1
)

echo.
echo   ds-port 正在启动，浏览器会自动打开
echo   用完关掉这个窗口就行
echo.

%PYCMD% server.py
if errorlevel 1 pause
