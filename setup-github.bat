@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   把新闻收集器推送到 GitHub
echo ============================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo   [错误] 没有检测到 Git
    echo.
    echo   请先安装：https://git-scm.com/download/win
    echo   安装时一路默认下一步即可。
    echo.
    pause
    exit /b 1
)

echo   请输入在 GitHub 上创建好的仓库信息。
echo   如果还没创建，先去 https://github.com/new 建一个空仓库。
echo.

set /p GHUSER=  你的 GitHub 用户名:
if "%GHUSER%"=="" (
    echo   用户名不能为空
    pause
    exit /b 1
)

set /p GHREPO=  仓库名（如 daily-news）:
if "%GHREPO%"=="" (
    echo   仓库名不能为空
    pause
    exit /b 1
)

echo.
echo   正在初始化...

if exist ".git" (
    echo   已有 git 仓库，跳过初始化
) else (
    git init -q
    git branch -M main
)

git add -A
git commit -q -m "初始化新闻收集器" 2>nul
if errorlevel 1 (
    echo   没有新改动需要提交
)

git remote remove origin >nul 2>&1
git remote add origin https://github.com/%GHUSER%/%GHREPO%.git

echo.
echo   即将推送到：https://github.com/%GHUSER%/%GHREPO%
echo   如果弹出登录窗口，选择用浏览器授权即可。
echo.

git push -u origin main
if errorlevel 1 (
    echo.
    echo   [推送失败] 常见原因：
    echo     1. 仓库还没有在 GitHub 上创建
    echo     2. 用户名或仓库名填错了
    echo     3. 登录授权没完成
    echo.
    echo   可以重新运行本脚本，或手动执行 git push
    echo.
) else (
    echo.
    echo   [推送成功]
    echo.
    echo   接下来还有一步：
    echo     打开仓库的 Settings - Pages
    echo     把 Source 改成 GitHub Actions
    echo.
    echo   之后去 Actions 标签点一次 Run workflow 测试。
    echo.
)
pause
