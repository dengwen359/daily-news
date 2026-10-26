@echo off
setlocal
cd /d "%~dp0"
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY=py"
if not defined PY if exist "C://Users//dengwen//.workbuddy//binaries//python//versions//3.13.12//python.exe" set "PY=C://Users//dengwen//.workbuddy//binaries//python//versions//3.13.12//python.exe"
if not defined PY (
  echo.
  echo 没找到 Python。请先安装 Python，或把 python.exe 所在目录加到 PATH。
  echo.
  pause
  exit /b 1
)
echo 正在安装桌面通知：每天 08:40 和 20:40 各弹一次。
echo.
"%PY%" install_notify.py
echo.
pause
