@echo off
set TASK_NAME=每日新闻简报

schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1
if %errorlevel%==0 (
    echo   定时任务已删除，程序文件仍保留在本地。
) else (
    echo   没有找到定时任务，或需要管理员权限。
)
echo.
pause
