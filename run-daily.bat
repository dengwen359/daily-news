@echo off
REM ============================================
REM  每日任务入口：抓新闻 + 生成网页看板
REM  供 Windows 计划任务调用（不带暂停）
REM ============================================

set SCRIPT_DIR=%~dp0
set PYTHON_EXE=C:\Users\dengwen\.workbuddy\binaries\python\versions\3.13.12\python.exe

"%PYTHON_EXE%" "%SCRIPT_DIR%collector.py"
"%PYTHON_EXE%" "%SCRIPT_DIR%build_site.py"

exit /b 0
