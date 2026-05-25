@echo off
chcp 65001 >nul 2>&1
title SOSReport RCA Engine

cd /d "%~dp0"

echo ========================================
echo   SOSReport RCA Engine V0.1
echo ========================================
echo.
echo 正在启动分析服务，浏览器将自动打开...
echo 分析完成后可关闭此窗口。
echo ========================================
echo.

python -u src\web.py

pause
