@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Xun-Qi 尋棲 - 第一次準備
echo ============================================================
echo   第一次準備（只需做一次）
echo   程式需要的東西都只放在這個資料夾，不會安裝到 Windows 系統。
echo   會依你的顯示卡建議版本，也可以自己選。之後想換版本，重跑 setup.bat 即可。
echo ============================================================
set "PY=%~dp0runtime\python\python.exe"
if exist "%PY%" goto detect

echo [1/5] 下載免安裝版 Python 3.11 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "New-Item -ItemType Directory -Force runtime | Out-Null;" ^
  "Invoke-WebRequest 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile 'runtime\py.zip';" ^
  "Expand-Archive 'runtime\py.zip' 'runtime\python' -Force; Remove-Item 'runtime\py.zip';" ^
  "(Get-Content 'runtime\python\python311._pth') -replace '#import site','import site' | Set-Content 'runtime\python\python311._pth';" ^
  "Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'runtime\get-pip.py'"
if not exist "%PY%" (
  echo [錯誤] Python 下載失敗，請確認網路後重新執行 setup.bat
  pause & exit /b 1
)
"%PY%" runtime\get-pip.py --no-warn-script-location

:detect
echo [2/5] 偵測顯示卡 ...
set "VARIANT="
set "GPU="
set "WHY="
for /f "usebackq tokens=1,2,3 delims=|" %%a in (`powershell -NoProfile -ExecutionPolicy Bypass -File "app\detect_gpu.ps1"`) do (
  set "VARIANT=%%a"
  set "GPU=%%b"
  set "WHY=%%c"
)
if "%VARIANT%"=="" set "VARIANT=cpu"
rem 手動指定：setup.bat [lite／standard／flagship] [cpu／cu126／cu128／cu130／xpu]
set "EDITION=%~1"
if not "%~2"=="" (
  set "VARIANT=%~2"
  set "WHY=manual"
)
echo     顯示卡：!GPU!
if "!WHY!"=="blackwell"          echo     NVIDIA RTX 50 系列 → 使用 CUDA 13.0 版（約 1.9GB）
if "!WHY!"=="blackwell_olddriver" echo     NVIDIA RTX 50 系列，驅動較舊 → 使用 CUDA 12.8 版（約 2.9GB）。建議更新驅動到 580 以上後重跑 setup.bat，可換成較小的版本。
if "!WHY!"=="nvidia"             echo     NVIDIA 顯示卡 → 使用 CUDA 12.6 版（約 2.6GB）
if "!WHY!"=="nvidia_old"         echo     較舊的 NVIDIA 顯示卡（GTX 10 系列或更早）→ 使用 CUDA 12.6 版，並固定在仍支援這張卡的 PyTorch 2.14
if "!WHY!"=="nvidia_too_old"     echo     這張 NVIDIA 顯示卡太舊，AI 改用 CPU（約 0.1GB）
if "!WHY!"=="nvidia_no_driver"   echo     找到 NVIDIA 顯示卡但沒有驅動程式 → 先用 CPU。裝好 NVIDIA 驅動後重跑 setup.bat 就會換成顯示卡版。
if "!WHY!"=="intel_arc"          echo     Intel Arc 顯示晶片 → 使用 Intel 版（約 0.6GB）。若之後分類出錯，可執行「setup.bat cpu」改回 CPU 版。
if "!WHY!"=="amd"                echo     AMD 顯示卡目前沒有穩定的 Windows 版 AI 套件 → 使用 CPU（約 0.1GB）
if "!WHY!"=="none"               echo     沒有獨立顯示卡 → 使用 CPU（約 0.1GB）
if "!WHY!"=="manual"             echo     手動指定版本：!VARIANT!

set "REC=2"
if /i "%VARIANT%"=="cpu" set "REC=1"
echo.
echo [3/5] 選擇版本（三個版本功能相同，差別在準確度、速度和大小）
echo     1. 輕量版：不需要 PyTorch，最省空間（約 0.8GB），任何電腦都能用，準確度略低
echo     2. 標準版：一般電腦建議，有 NVIDIA／Intel 顯示卡會用顯示卡
echo     3. 旗艦版：最準的文字辨識、影片每 2 秒一格，適合有獨立顯示卡的電腦
echo     建議：!REC!
if /i "%EDITION%"=="lite" goto ed_ok
if /i "%EDITION%"=="standard" goto ed_ok
if /i "%EDITION%"=="flagship" goto ed_ok
set "PICK="
set /p "PICK=請輸入 1、2 或 3，直接按 Enter 使用建議（!REC!）："
if "!PICK!"=="" set "PICK=!REC!"
set "EDITION=standard"
if "!PICK!"=="1" set "EDITION=lite"
if "!PICK!"=="3" set "EDITION=flagship"
:ed_ok
echo     版本：%EDITION%
>"runtime\edition.txt" echo %EDITION%
if /i "%EDITION%"=="lite" goto lite

set "IDX=https://download.pytorch.org/whl/cpu"
set "PIN=torch"
if /i "%VARIANT%"=="cu126"    set "IDX=https://download.pytorch.org/whl/cu126"
if /i "%VARIANT%"=="cu126old" (
  set "IDX=https://download.pytorch.org/whl/cu126"
  set "PIN=torch<2.15"
)
if /i "%VARIANT%"=="cu128"    set "IDX=https://download.pytorch.org/whl/cu128"
if /i "%VARIANT%"=="cu130"    set "IDX=https://download.pytorch.org/whl/cu130"
if /i "%VARIANT%"=="xpu"      set "IDX=https://download.pytorch.org/whl/xpu"

set "OLD="
if exist "runtime\torch_variant.txt" set /p OLD=<"runtime\torch_variant.txt"
set "FORCE="
if not "%OLD%"=="" if /i not "%OLD%"=="%VARIANT%" (
  echo     之前裝的是 %OLD% 版，改裝 %VARIANT% 版 ...
  "%PY%" -m pip uninstall -y torch torchvision >nul 2>&1
)

echo [4/5] 安裝 PyTorch（%VARIANT%）...
"%PY%" -m pip install --no-warn-script-location "%PIN%" torchvision --index-url %IDX%
if errorlevel 1 ( echo [錯誤] PyTorch 安裝失敗，請確認網路後重新執行 setup.bat & pause & exit /b 1 )
>"runtime\torch_variant.txt" echo %VARIANT%

echo [5/5] 安裝其他套件 ...
"%PY%" -m pip install --no-warn-script-location -r requirements.txt -r requirements-torch.txt
if errorlevel 1 ( echo [錯誤] 套件安裝失敗 & pause & exit /b 1 )

rem OCR（照片文字）用的 onnxruntime：NVIDIA CUDA 12 版用顯示卡版本（共用 PyTorch 的 CUDA），其他用 CPU 版
set "ORT=onnxruntime"
if /i "%VARIANT%"=="cu126"    set "ORT=onnxruntime-gpu"
if /i "%VARIANT%"=="cu126old" set "ORT=onnxruntime-gpu"
if /i "%VARIANT%"=="cu128"    set "ORT=onnxruntime-gpu"
echo     安裝 OCR 執行套件（%ORT%）...
"%PY%" -m pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-directml >nul 2>&1
"%PY%" -m pip install --no-warn-script-location "%ORT%"
if errorlevel 1 echo [提醒] OCR 套件安裝失敗，照片文字搜尋暫時不能用，其他功能不受影響。
"%PY%" -m pip cache purge >nul 2>&1

echo.
"%PY%" app\check_gpu.py
goto finish

:lite
rem 輕量版：不裝 PyTorch（之前裝過就移除，省下 2GB 以上），AI 和 OCR 都用 onnxruntime-directml
if exist "runtime\torch_variant.txt" (
  echo     移除之前安裝的 PyTorch ...
  "%PY%" -m pip uninstall -y torch torchvision sentence-transformers transformers accelerate >nul 2>&1
  del "runtime\torch_variant.txt" >nul 2>&1
)
echo [4/5] 安裝套件 ...
"%PY%" -m pip install --no-warn-script-location -r requirements.txt
if errorlevel 1 ( echo [錯誤] 套件安裝失敗 & pause & exit /b 1 )
echo [5/5] 安裝 AI 執行套件（onnxruntime-directml，可用任何品牌的顯示卡，沒有就用 CPU）...
"%PY%" -m pip uninstall -y onnxruntime onnxruntime-gpu >nul 2>&1
"%PY%" -m pip install --no-warn-script-location onnxruntime-directml
if errorlevel 1 (
  echo     DirectML 版安裝失敗，改裝 CPU 版 ...
  "%PY%" -m pip install --no-warn-script-location onnxruntime
  if errorlevel 1 ( echo [錯誤] 套件安裝失敗 & pause & exit /b 1 )
)
"%PY%" -m pip cache purge >nul 2>&1
echo.
"%PY%" app\check_gpu.py lite

:finish
echo.
echo 完成！下一步：執行 download_model.bat 下載 AI 模型和 OCR 模型（已自己下載好的話可跳過）。
pause
