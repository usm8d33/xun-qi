@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Xun-Qi 尋棲（關閉此視窗即結束）
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" (
  echo 第一次使用請先執行 setup.bat
  pause & exit /b 1
)
set PYTHONIOENCODING=utf-8
set HF_HUB_OFFLINE=1
"%PY%" app\main.py
pause
