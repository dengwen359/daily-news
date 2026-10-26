@echo off
set SCRIPT_DIR=%~dp0
set PYTHON_EXE=C:\Users\dengwen\.workbuddy\binaries\python\versions\3.13.12\python.exe

echo.
echo   正在抓取今天的新闻...
echo.
"%PYTHON_EXE%" "%SCRIPT_DIR%collector.py"

echo.
echo   正在生成网页看板...
"%PYTHON_EXE%" "%SCRIPT_DIR%build_site.py"

echo.
echo   完成。日报在 reports 文件夹，网页是 docs\index.html
echo.
pause
