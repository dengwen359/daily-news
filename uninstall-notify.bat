@echo off
setlocal
schtasks /Delete /TN "每日新闻·桌面通知" /F >nul 2>nul
schtasks /Delete /TN "每日新闻·开机补弹" /F >nul 2>nul
schtasks /Delete /TN "每日新闻·桌面通知（早）" /F >nul 2>nul
schtasks /Delete /TN "每日新闻·桌面通知（晚）" /F >nul 2>nul
del "%USERPROFILE%\Desktop\今日新闻.url" >nul 2>nul
echo 已取消每天的桌面通知，并删除桌面快捷方式。
echo 项目文件都还在，想恢复重新双击 install-notify.bat。
echo.
pause
