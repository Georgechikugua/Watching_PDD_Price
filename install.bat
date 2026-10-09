@echo off
chcp 65001 >nul
title 价格盯盘 - 环境安装

echo ==============================================
echo   价格盯盘 - 一键安装(需要已安装 Python 3.10+)
echo   下载 Python: https://www.python.org/downloads/
echo   安装时记得勾选 "Add Python to PATH"
echo ==============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python,请先安装并勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

echo [1/3] 安装依赖库...
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :fail

echo [2/3] 下载浏览器内核(约 150MB,已走国内镜像)...
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
python -m playwright install chromium
if errorlevel 1 goto :fail

echo [3/3] 进入初始设置向导...
python "%~dp0tracker.py" setup

echo.
echo 全部完成!以后双击 install.bat 也可以重新运行向导。
pause
exit /b 0

:fail
echo.
echo [错误] 安装失败,请截图上面的报错信息寻求帮助。
pause
exit /b 1
