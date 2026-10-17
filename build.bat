@echo off
chcp 65001 >nul
title DG-Control Hub Build

echo [1/2] 安装依赖（首次构建请耐心等待）...
pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [2/2] PyInstaller 单文件打包...
pyinstaller ^
    --noconsole ^
    --windowed ^
    --onefile ^
    --clean ^
    --icon="icon.ico" ^
    --name="DG-Control-Hub" ^
    --add-data="web;web" ^
    --collect-submodules webview ^
    app.py
if errorlevel 1 goto :fail

echo.
echo 构建完成：dist\DG-Control-Hub.exe （单文件绿色免安装，双击即用）
pause
exit /b 0

:fail
echo.
echo 构建失败，请检查上方错误信息。
pause
exit /b 1
