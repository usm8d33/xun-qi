@echo off
rem 決定 setup.bat／download_model.bat／start.bat 顯示的語言，結果放在 XQL（zh 或 en）
rem 順序：環境變數 XQ_LANG → user_settings.json 的 "lang" → Windows 介面語言（中文 → zh，其他 → en）
set "XQL=%XQ_LANG%"
if not defined XQL for /f "usebackq delims=" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lang.ps1"`) do set "XQL=%%L"
if /i "%XQL:~0,2%"=="zh" (set "XQL=zh") else (set "XQL=en")
