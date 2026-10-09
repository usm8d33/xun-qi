@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
call app\lang.cmd
set "XQ_LANG=%XQL%"
if /i "%XQL%"=="en" goto msg_en
set "M_TITLE=Xun-Qi 尋棲 - 第一次準備"
set "M_H1=  第一次準備（只需做一次）"
set "M_H2=  程式需要的東西都只放在這個資料夾，不會安裝到 Windows 系統。"
set "M_H3=  會依你的顯示卡建議版本，也可以自己選。之後想換版本，重跑 setup.bat 即可。"
set "M_S1=[1/5] 下載免安裝版 Python 3.11 ..."
set "M_PYFAIL=[錯誤] Python 下載失敗，請確認網路後重新執行 setup.bat"
set "M_S2=[2/5] 偵測顯示卡 ..."
set "M_GPU=    顯示卡："
set "M_W_BLACKWELL=    NVIDIA RTX 50 系列 → 使用 CUDA 13.0 版（約 1.9GB）"
set "M_W_BLACKWELL_OLD=    NVIDIA RTX 50 系列，驅動較舊 → 使用 CUDA 12.8 版（約 2.9GB）。建議更新驅動到 580 以上後重跑 setup.bat，可換成較小的版本。"
set "M_W_NVIDIA=    NVIDIA 顯示卡 → 使用 CUDA 12.6 版（約 2.6GB）"
set "M_W_NVIDIA_OLD=    較舊的 NVIDIA 顯示卡（GTX 10 系列或更早）→ 使用 CUDA 12.6 版，並固定在仍支援這張卡的 PyTorch 2.14"
set "M_W_NVIDIA_TOO_OLD=    這張 NVIDIA 顯示卡太舊，AI 改用 CPU（約 0.1GB）"
set "M_W_NVIDIA_NO_DRIVER=    找到 NVIDIA 顯示卡但沒有驅動程式 → 先用 CPU。裝好 NVIDIA 驅動後重跑 setup.bat 就會換成顯示卡版。"
set "M_W_INTEL=    Intel Arc 顯示晶片 → 使用 Intel 版（約 0.6GB）。若之後分類出錯，可執行「setup.bat cpu」改回 CPU 版。"
set "M_W_AMD=    AMD 顯示卡目前沒有穩定的 Windows 版 AI 套件 → 使用 CPU（約 0.1GB）"
set "M_W_NONE=    沒有獨立顯示卡 → 使用 CPU（約 0.1GB）"
set "M_W_MANUAL=    手動指定版本："
set "M_S3=[3/5] 選擇版本（三個版本功能相同，差別在準確度、速度和大小）"
set "M_E1=    1. 輕量版：不需要 PyTorch，最省空間（約 0.8GB），任何電腦都能用，準確度略低"
set "M_E2=    2. 標準版：一般電腦建議，有 NVIDIA／Intel 顯示卡會用顯示卡"
set "M_E3=    3. 旗艦版：最準的文字辨識、影片每 2 秒一格，適合有獨立顯示卡的電腦"
set "M_REC=    建議："
set "M_PICK1=請輸入 1、2 或 3，直接按 Enter 使用建議（"
set "M_PICK2=）："
set "M_ED=    版本："
set "M_OLD1=    之前裝的是 "
set "M_OLD2= 版，改裝 "
set "M_OLD3= 版 ..."
set "M_S4T=[4/5] 安裝 PyTorch（"
set "M_S4T2=）..."
set "M_TORCHFAIL=[錯誤] PyTorch 安裝失敗，請確認網路後重新執行 setup.bat"
set "M_S5=[5/5] 安裝其他套件 ..."
set "M_PKGFAIL=[錯誤] 套件安裝失敗"
set "M_ORT1=    安裝 OCR 執行套件（"
set "M_ORT2=）..."
set "M_ORTFAIL=[提醒] OCR 套件安裝失敗，照片文字搜尋暫時不能用，其他功能不受影響。"
set "M_RMTORCH=    移除之前安裝的 PyTorch ..."
set "M_S4L=[4/5] 安裝套件 ..."
set "M_S5L=[5/5] 安裝 AI 執行套件（onnxruntime-directml，可用任何品牌的顯示卡，沒有就用 CPU）..."
set "M_DMLFAIL=    DirectML 版安裝失敗，改裝 CPU 版 ..."
set "M_DONE=完成！下一步：執行 download_model.bat 下載 AI 模型和 OCR 模型（已自己下載好的話可跳過）。"
goto msg_done
:msg_en
set "M_TITLE=Xun-Qi - First-time setup"
set "M_H1=  First-time setup (only needed once)"
set "M_H2=  Everything the app needs stays in this folder. Nothing is installed into Windows."
set "M_H3=  An edition is suggested for your graphics card, or choose your own. To switch later, run setup.bat again."
set "M_S1=[1/5] Downloading portable Python 3.11 ..."
set "M_PYFAIL=[Error] Couldn't download Python. Check your internet connection and run setup.bat again."
set "M_S2=[2/5] Detecting graphics card ..."
set "M_GPU=    Graphics card: "
set "M_W_BLACKWELL=    NVIDIA RTX 50 series -> CUDA 13.0 build (about 1.9 GB)"
set "M_W_BLACKWELL_OLD=    NVIDIA RTX 50 series with an older driver -> CUDA 12.8 build (about 2.9 GB). Update the driver to 580 or later and run setup.bat again for a smaller build."
set "M_W_NVIDIA=    NVIDIA graphics -> CUDA 12.6 build (about 2.6 GB)"
set "M_W_NVIDIA_OLD=    Older NVIDIA graphics (GTX 10 series or earlier) -> CUDA 12.6 build, pinned to PyTorch 2.14, which still supports this card"
set "M_W_NVIDIA_TOO_OLD=    This NVIDIA card is too old, so the AI uses the CPU (about 0.1 GB)"
set "M_W_NVIDIA_NO_DRIVER=    NVIDIA card found but no driver -> using the CPU for now. Install the NVIDIA driver and run setup.bat again to use the graphics card."
set "M_W_INTEL=    Intel Arc graphics -> Intel build (about 0.6 GB). If sorting fails later, run setup.bat cpu to switch to the CPU build."
set "M_W_AMD=    AMD graphics don't have a stable Windows AI package yet -> using the CPU (about 0.1 GB)"
set "M_W_NONE=    No dedicated graphics card -> using the CPU (about 0.1 GB)"
set "M_W_MANUAL=    Manually selected build: "
set "M_S3=[3/5] Choose an edition (all three have the same features; they differ in accuracy, speed and size)"
set "M_E1=    1. Lite: no PyTorch, smallest (about 0.8 GB), runs on any PC, slightly less accurate"
set "M_E2=    2. Standard: recommended for most PCs; uses NVIDIA or Intel graphics if available"
set "M_E3=    3. Flagship: most accurate text recognition, a video frame every 2 seconds; for PCs with a dedicated graphics card"
set "M_REC=    Suggested: "
set "M_PICK1=Enter 1, 2 or 3, or press Enter for the suggestion ("
set "M_PICK2=): "
set "M_ED=    Edition: "
set "M_OLD1=    Previously installed: "
set "M_OLD2=, switching to "
set "M_OLD3= ..."
set "M_S4T=[4/5] Installing PyTorch ("
set "M_S4T2=) ..."
set "M_TORCHFAIL=[Error] Couldn't install PyTorch. Check your internet connection and run setup.bat again."
set "M_S5=[5/5] Installing other packages ..."
set "M_PKGFAIL=[Error] Couldn't install packages"
set "M_ORT1=    Installing the OCR runtime ("
set "M_ORT2=) ..."
set "M_ORTFAIL=[Note] Couldn't install the OCR package. Searching text in photos won't work for now; everything else is fine."
set "M_RMTORCH=    Removing the previously installed PyTorch ..."
set "M_S4L=[4/5] Installing packages ..."
set "M_S5L=[5/5] Installing the AI runtime (onnxruntime-directml; works with any graphics card, or the CPU) ..."
set "M_DMLFAIL=    Couldn't install the DirectML build, installing the CPU build ..."
set "M_DONE=Done. Next, run download_model.bat to download the AI and OCR models (skip it if you already downloaded them)."
:msg_done
title !M_TITLE!
echo ============================================================
echo !M_H1!
echo !M_H2!
echo !M_H3!
echo ============================================================
set "PY=%~dp0runtime\python\python.exe"
if exist "%PY%" goto detect

echo !M_S1!
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "New-Item -ItemType Directory -Force runtime | Out-Null;" ^
  "Invoke-WebRequest 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile 'runtime\py.zip';" ^
  "Expand-Archive 'runtime\py.zip' 'runtime\python' -Force; Remove-Item 'runtime\py.zip';" ^
  "(Get-Content 'runtime\python\python311._pth') -replace '#import site','import site' | Set-Content 'runtime\python\python311._pth';" ^
  "Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'runtime\get-pip.py'"
if not exist "%PY%" (
  echo !M_PYFAIL!
  pause & exit /b 1
)
"%PY%" runtime\get-pip.py --no-warn-script-location

:detect
echo !M_S2!
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
echo !M_GPU!!GPU!
if "!WHY!"=="blackwell"          echo !M_W_BLACKWELL!
if "!WHY!"=="blackwell_olddriver" echo !M_W_BLACKWELL_OLD!
if "!WHY!"=="nvidia"             echo !M_W_NVIDIA!
if "!WHY!"=="nvidia_old"         echo !M_W_NVIDIA_OLD!
if "!WHY!"=="nvidia_too_old"     echo !M_W_NVIDIA_TOO_OLD!
if "!WHY!"=="nvidia_no_driver"   echo !M_W_NVIDIA_NO_DRIVER!
if "!WHY!"=="intel_arc"          echo !M_W_INTEL!
if "!WHY!"=="amd"                echo !M_W_AMD!
if "!WHY!"=="none"               echo !M_W_NONE!
if "!WHY!"=="manual"             echo !M_W_MANUAL!!VARIANT!

set "REC=2"
if /i "%VARIANT%"=="cpu" set "REC=1"
echo.
echo !M_S3!
echo !M_E1!
echo !M_E2!
echo !M_E3!
echo !M_REC!!REC!
if /i "%EDITION%"=="lite" goto ed_ok
if /i "%EDITION%"=="standard" goto ed_ok
if /i "%EDITION%"=="flagship" goto ed_ok
set "PICK="
set /p "PICK=!M_PICK1!!REC!!M_PICK2!"
if "!PICK!"=="" set "PICK=!REC!"
set "EDITION=standard"
if "!PICK!"=="1" set "EDITION=lite"
if "!PICK!"=="3" set "EDITION=flagship"
:ed_ok
echo !M_ED!%EDITION%
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
  echo !M_OLD1!%OLD%!M_OLD2!%VARIANT%!M_OLD3!
  "%PY%" -m pip uninstall -y torch torchvision >nul 2>&1
)

echo !M_S4T!%VARIANT%!M_S4T2!
"%PY%" -m pip install --no-warn-script-location "%PIN%" torchvision --index-url %IDX%
if errorlevel 1 ( echo !M_TORCHFAIL! & pause & exit /b 1 )
>"runtime\torch_variant.txt" echo %VARIANT%

echo !M_S5!
"%PY%" -m pip install --no-warn-script-location -r requirements.txt -r requirements-torch.txt
if errorlevel 1 ( echo !M_PKGFAIL! & pause & exit /b 1 )

rem OCR（照片文字）用的 onnxruntime：NVIDIA CUDA 12 版用顯示卡版本（共用 PyTorch 的 CUDA），其他用 CPU 版
set "ORT=onnxruntime"
if /i "%VARIANT%"=="cu126"    set "ORT=onnxruntime-gpu"
if /i "%VARIANT%"=="cu126old" set "ORT=onnxruntime-gpu"
if /i "%VARIANT%"=="cu128"    set "ORT=onnxruntime-gpu"
echo !M_ORT1!%ORT%!M_ORT2!
"%PY%" -m pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-directml >nul 2>&1
"%PY%" -m pip install --no-warn-script-location "%ORT%"
if errorlevel 1 echo !M_ORTFAIL!
"%PY%" -m pip cache purge >nul 2>&1

echo.
"%PY%" app\check_gpu.py
goto finish

:lite
rem 輕量版：不裝 PyTorch（之前裝過就移除，省下 2GB 以上），AI 和 OCR 都用 onnxruntime-directml
if exist "runtime\torch_variant.txt" (
  echo !M_RMTORCH!
  "%PY%" -m pip uninstall -y torch torchvision sentence-transformers transformers accelerate >nul 2>&1
  del "runtime\torch_variant.txt" >nul 2>&1
)
echo !M_S4L!
"%PY%" -m pip install --no-warn-script-location -r requirements.txt
if errorlevel 1 ( echo !M_PKGFAIL! & pause & exit /b 1 )
echo !M_S5L!
"%PY%" -m pip uninstall -y onnxruntime onnxruntime-gpu >nul 2>&1
"%PY%" -m pip install --no-warn-script-location onnxruntime-directml
if errorlevel 1 (
  echo !M_DMLFAIL!
  "%PY%" -m pip install --no-warn-script-location onnxruntime
  if errorlevel 1 ( echo !M_PKGFAIL! & pause & exit /b 1 )
)
"%PY%" -m pip cache purge >nul 2>&1
echo.
"%PY%" app\check_gpu.py lite

:finish
echo.
echo !M_DONE!
pause
