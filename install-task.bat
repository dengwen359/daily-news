@echo off
REM ============================================
REM  每日新闻收集器 - 一键安装定时任务
REM  双击运行即可，之后每天自动收集
REM ============================================

set SCRIPT_DIR=%~dp0
set PYTHON_EXE=C:\Users\dengwen\.workbuddy\binaries\python\versions\3.13.12\python.exe
set TASK_NAME=每日新闻简报

echo.
echo   安装目录：%SCRIPT_DIR%
echo   定时时间：每天 08:30
echo.

schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if %errorlevel%==0 (
    echo   检测到已有任务，正在更新...
    schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
)

schtasks /Create ^
 /TN "%TASK_NAME%" ^
 /TR "\"%SCRIPT_DIR%run-daily.bat\"" ^
 /SC DAILY ^
 /ST 08:30 ^
 /RL HIGHEST ^
 /F

if %errorlevel%==0 (
    echo.
    echo   [成功] 定时任务已创建
    echo.
    echo   - 每天 08:30 自动运行
    echo   - 日报位置：%SCRIPT_DIR%reports\
    echo   - 网页看板：%SCRIPT_DIR%docs\index.html
    echo   - 立即测试：双击 run-now.bat
    echo   - 卸载任务：双击 uninstall.bat
    echo.
) else (
    echo.
    echo   [失败] 请右键本文件，选择"以管理员身份运行"
    echo.
)
pause
